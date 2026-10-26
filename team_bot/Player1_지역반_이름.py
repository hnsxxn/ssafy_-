"""Battle SSAFY bot. Configure Rules against your version's official HELP first.

Local connection: python battle_bot.py (standard library only).
Offline action: python battle_bot.py --offline snapshot.txt [--rules rules.json].
Rules: uploaded v1.1.10 manual and Team Battle slides. No win guarantee.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import dataclass, fields
import heapq
import itertools
import json
from pathlib import Path
import sys
import socket
import select
import time

NICKNAME = "지역반_이름1"  # 제출 전 본인 닉네임으로 수정 (12자 이내)
PLAYER_SLOT = 1  # Player1/2/3 각각 1, 2, 3

DIRS = {"U": (-1, 0), "R": (0, 1), "D": (1, 0), "L": (0, -1)}
Pos = tuple[int, int]


@dataclass(frozen=True)
class Rules:
    # v1.1.10 manual pp.61-67 plus the supplied Team Battle rules.
    shoot_range: int = 3
    normal_damage: int = 30
    mega_damage: int = 70
    enemy_damage: int = 70  # Enemy ammunition is hidden: assume one mega hit.
    sand_damage: int = 10
    tree_shots: int = 1  # This implementation supports one-shot trees only.
    mega_command: str = "F M"  # Some versions use "F S".
    caesar_shift: int | None = 17  # Manual p.66: SRKKCVJJRWP -> BATTLESSAFY.
    supply_gain: int = 1
    mega_capacity: int = 10
    own_symbol: str | None = None
    target_symbol: str = "X"
    base_symbol: str = "H"
    team_mode: bool = True
    expected_enemy_tanks: int = 3
    base_exclusion_radius: int = 1
    player_slot: int = 1
    max_nodes: int = 3500
    time_budget_ms: int = 140
    hp_cost: float = 0.08
    next_move_risk_cost: float = 0.12

    def __post_init__(self):
        for key in ("shoot_range", "normal_damage", "mega_damage", "supply_gain",
                    "mega_capacity", "max_nodes", "time_budget_ms"):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")
        if self.player_slot not in (1, 2, 3):
            raise ValueError("player_slot must be 1, 2 or 3")
        if self.expected_enemy_tanks < 1 or self.base_exclusion_radius < 0:
            raise ValueError("Invalid team configuration")
        if self.tree_shots != 1:
            raise ValueError("Only one-shot trees are supported; check official HELP")
        if self.mega_command not in ("F M", "F S"):
            raise ValueError("mega_command must be 'F M' or 'F S'")
        if min(self.enemy_damage, self.sand_damage, self.hp_cost,
               self.next_move_risk_cost) < 0:
            raise ValueError("Damage and cost values must be nonnegative")


def parse_data(raw: str) -> dict:
    """Parse the common 5-field header format, including CRLF/multiple spaces."""
    lines = raw.splitlines()
    if not lines:
        raise ValueError("Empty game data")
    header = lines[0].split()
    if len(header) != 5:
        raise ValueError("Expected: height width allies enemies codes")
    height, width, na, ne, nc = map(int, header)
    if height <= 0 or width <= 0 or min(na, ne, nc) < 0:
        raise ValueError("Invalid header counts")
    expected = 1 + height + na + ne + nc
    if len(lines) < expected:
        raise ValueError("Truncated game data")
    grid = [line.split() for line in lines[1:1 + height]]
    if any(len(row) != width for row in grid):
        raise ValueError("Map width mismatch")
    index = 1 + height
    groups = []
    for count in (na, ne):
        group = {}
        for line in lines[index:index + count]:
            tokens = line.split()
            if len(tokens) < 2 or tokens[0] in group:
                raise ValueError("Invalid or duplicate unit entry")
            group[tokens[0]] = tokens[1:]
        groups.append(group)
        index += count
    return {"grid": grid, "allies": groups[0], "enemies": groups[1],
            "codes": [line.strip() for line in lines[index:index + nc]]}


def decode_caesar(code: str, shift: int) -> str:
    out = []
    for ch in code:
        base = ord("A") if "A" <= ch <= "Z" else ord("a") if "a" <= ch <= "z" else None
        out.append(chr((ord(ch) - base - shift) % 26 + base) if base is not None else ch)
    return "".join(out)


# Search node: position, hp, normal ammo, special ammo, removed-tree mask, enemy HPs.
@dataclass(frozen=True)
class Node:
    pos: Pos
    hp: int
    normal: int
    mega: int
    cleared: int
    enemy_hp: tuple[int, ...]


class World:
    def __init__(self, state: dict, rules: Rules):
        self.rules = rules
        self.grid = state["grid"]
        self.height = len(self.grid)
        self.width = len(self.grid[0])
        self.positions = {cell: (r, c) for r, row in enumerate(self.grid)
                          for c, cell in enumerate(row) if cell not in ("G", "S", "W", "R", "T", "F")}
        candidates = [key for key, value in state["allies"].items()
                      if len(value) >= 4 and str(value[1]) in DIRS and key in self.positions]
        self.own = rules.own_symbol
        if self.own is None:
            if len(candidates) != 1:
                raise ValueError("Cannot identify own tank: set own_symbol in rules.json")
            self.own = candidates[0]
        if self.own not in candidates:
            raise ValueError("Own tank is missing or does not have hp/direction/ammo fields")
        stats = state["allies"][self.own]
        hp, normal, mega = int(stats[0]), int(stats[2]), int(stats[3])
        self.enemy_names = tuple(sorted(name for name in state["enemies"] if name in self.positions))
        self.enemy_index = {name: i for i, name in enumerate(self.enemy_names)}
        self.trees = {(r, c): 1 << index for index, (r, c) in enumerate(
            ( (r, c) for r, row in enumerate(self.grid) for c, cell in enumerate(row) if cell == "T"))}
        self.start = Node(self.positions[self.own], hp, normal, mega, 0,
                          tuple(int(state["enemies"][name][0]) for name in self.enemy_names))
        if min(hp, normal, mega, *self.start.enemy_hp) < 0:
            raise ValueError("Negative HP or ammunition")
        self.base = self.positions.get(rules.base_symbol)
        self.ally_positions = [self.positions[k] for k in state["allies"]
                               if k not in (self.own, rules.base_symbol) and k in self.positions]
        self.has_supply = any("F" in row for row in self.grid)
        # Never infer a key using English letter frequency: short codes are ambiguous.
        self.supply_command = None
        if rules.caesar_shift is not None and state.get("codes"):
            decoded = decode_caesar(state["codes"][0], rules.caesar_shift)
            if decoded and len(decoded.split()) == 1:
                self.supply_command = "G " + decoded

    def inside(self, pos: Pos) -> bool:
        return 0 <= pos[0] < self.height and 0 <= pos[1] < self.width

    def cell(self, pos: Pos, node: Node) -> str:
        cell = self.grid[pos[0]][pos[1]]
        if cell == self.own:
            return "G"
        if cell == "T" and node.cleared & self.trees[pos]:
            return "G"
        index = self.enemy_index.get(cell)
        if index is not None and node.enemy_hp[index] <= 0:
            return "G"
        return cell

    def ray(self, pos: Pos, direction: str, node: Node):
        dr, dc = DIRS[direction]
        for step in range(1, self.rules.shoot_range + 1):
            nxt = pos[0] + dr * step, pos[1] + dc * step
            if not self.inside(nxt):
                return None
            cell = self.cell(nxt, node)
            if cell not in ("G", "W", "S"):
                return nxt, cell
        return None

    def sees(self, source: Pos, target: Pos, node: Node) -> bool:
        if source == target:
            return False
        dr, dc = target[0] - source[0], target[1] - source[1]
        if dr and dc or abs(dr) + abs(dc) > self.rules.shoot_range:
            return False
        sr, sc = (dr > 0) - (dr < 0), (dc > 0) - (dc < 0)
        for k in range(1, abs(dr) + abs(dc)):
            pos = source[0] + sr * k, source[1] + sc * k
            if self.cell(pos, node) not in ("G", "W", "S"):
                return False
        return True

    def adjacent_supply(self, pos: Pos) -> bool:
        return any(self.inside((pos[0] + dr, pos[1] + dc))
                   and self.grid[pos[0] + dr][pos[1] + dc] == "F" for dr, dc in DIRS.values())

    def movable(self, pos: Pos, node: Node) -> bool:
        if not self.inside(pos) or self.cell(pos, node) not in ("G", "S"):
            return False
        if self.rules.team_mode and self.base:
            if max(abs(pos[0] - self.base[0]), abs(pos[1] - self.base[1])) <= self.rules.base_exclusion_radius:
                return False
        return True

    def victory(self, node: Node) -> bool:
        turret = self.enemy_index.get(self.rules.target_symbol)
        if turret is not None and node.enemy_hp[turret] <= 0:
            return True
        if not self.rules.team_mode:
            return False
        living = sum(hp > 0 and name != self.rules.target_symbol
                     for name, hp in zip(self.enemy_names, node.enemy_hp))
        return self.rules.expected_enemy_tanks - living >= min(2, self.rules.expected_enemy_tanks)

    def incoming(self, node: Node) -> int:
        return self.rules.enemy_damage * sum(
            node.enemy_hp[i] > 0 and name != self.rules.target_symbol
            and self.sees(self.positions[name], node.pos, node)
            for i, name in enumerate(self.enemy_names))

    def future_risk(self, node: Node) -> float:
        """Possible one-move enemy firing positions; cost only, not certain damage."""
        risk = 0.0
        for i, name in enumerate(self.enemy_names):
            if node.enemy_hp[i] <= 0 or name == self.rules.target_symbol:
                continue
            origin = self.positions[name]
            if self.sees(origin, node.pos, node):
                continue
            for dr, dc in DIRS.values():
                nxt = origin[0] + dr, origin[1] + dc
                if (self.inside(nxt) and nxt != node.pos
                        and self.cell(nxt, node) in ("G", "S")
                        and self.sees(nxt, node.pos, node)):
                    risk += 1
                    break
        return risk

    def successors(self, node: Node, target: int, root: bool):
        rules = self.rules

        def after(action, pos, normal, mega, cleared, enemy_hp, terrain_damage=0):
            nxt = Node(pos, node.hp - terrain_damage, normal, mega, cleared, enemy_hp)
            # Winning damage ends the game before retaliation in this example profile.
            # A defensive sub-goal must still survive retaliation from other enemies.
            won = self.victory(nxt)
            damage = 0 if won else self.incoming(nxt)
            nxt = Node(pos, nxt.hp - damage, normal, mega, cleared, enemy_hp)
            cost = 1.0 + (terrain_damage + damage) * rules.hp_cost
            cost += self.future_risk(nxt) * rules.next_move_risk_cost
            return action, nxt, cost

        for direction, (dr, dc) in DIRS.items():
            pos = node.pos[0] + dr, node.pos[1] + dc
            if self.movable(pos, node):
                damage = rules.sand_damage if self.cell(pos, node) == "S" else 0
                item = after(direction + " A", pos, node.normal, node.mega,
                             node.cleared, node.enemy_hp, damage)
                if item:
                    yield item
            hit = self.ray(node.pos, direction, node)
            if not hit:
                continue
            pos, cell = hit
            index = self.enemy_index.get(cell)
            if cell != "T" and index is None:
                continue  # Never deliberately fire into an ally, rock or supply.
            for special, count, damage, command in (
                (False, node.normal, rules.normal_damage, "F"),
                (True, node.mega, rules.mega_damage, rules.mega_command),
            ):
                if count <= 0:
                    continue
                # Both shell types clear one tree; preserve the stronger shell when possible.
                if cell == "T" and special and node.normal > 0:
                    continue
                hp = list(node.enemy_hp)
                mask = node.cleared
                if cell == "T":
                    mask |= self.trees[pos]
                else:
                    hp[index] = max(0, hp[index] - damage)
                item = after(direction + " " + command, node.pos,
                             node.normal - (not special), node.mega - special, mask, tuple(hp))
                if item:
                    yield item
        # In future states, a placeholder denotes a supply action that will be decoded
        # from that turn's actual code. It can NEVER be emitted to the bridge.
        if (rules.caesar_shift is not None and self.adjacent_supply(node.pos)
                and node.mega < rules.mega_capacity and (not root or self.supply_command)):
            item = after(self.supply_command if root else "@SUPPLY", node.pos,
                         node.normal, min(rules.mega_capacity, node.mega + rules.supply_gain),
                         node.cleared, node.enemy_hp)
            if item:
                yield item


class BattleBot:
    def __init__(self, rules: Rules | None = None):
        self.rules = rules or Rules()
        self.recent = deque(maxlen=10)
        self.reason = ""
        self.expanded = 0

    def choose_action(self, state: dict) -> str:
        world = World(state, self.rules)
        start = world.start
        turret = world.enemy_index.get(self.rules.target_symbol)
        if start.hp <= 0 or turret is None or start.enemy_hp[turret] <= 0:
            self.reason = "No living player or visible enemy turret"
            return "S"
        immediate = list(world.successors(start, turret, root=True))
        wins = [item for item in immediate if world.victory(item[1])]
        if wins:
            self.reason = "Immediate team victory"
            return min(wins, key=lambda item: (start.mega - item[1].mega, item[2]))[0]
        safe = [item for item in immediate if item[1].hp > 0]
        # Focus fire: finish a damaged tank, but do not sacrifice ourselves for one kill.
        kills = [item for item in safe if any(a > 0 and b <= 0 for i, (a, b) in
                 enumerate(zip(start.enemy_hp, item[1].enemy_hp)) if i != turret)]
        if kills:
            self.reason = "Safe tank elimination"
            return min(kills, key=lambda item: (item[2], start.mega - item[1].mega))[0]
        # One mega + one normal destroys a full-health tank. Stock at most two
        # before advancing; collecting ten delays the team for little benefit.
        if world.supply_command and start.mega < 2 and world.adjacent_supply(start.pos):
            supplies = [item for item in safe if item[0].startswith("G ")]
            under_fire = world.incoming(start) > 0
            if supplies and not under_fire:
                self.reason = "Prepare mega ammunition for two-shot kills"
                return supplies[0][0]
        tank_indices = [i for i, name in enumerate(world.enemy_names)
                        if i != turret and start.enemy_hp[i] > 0]

        def target_priority(i):
            pos = world.positions[world.enemy_names[i]]
            distance = max(0, sum(abs(a-b) for a, b in zip(start.pos, pos)) - self.rules.shoot_range)
            hp = start.enemy_hp[i]
            damage = self.rules.mega_damage if start.mega else self.rules.normal_damage
            shots = (hp + damage - 1) // damage
            base_threat = 0
            if world.base and i != turret and world.sees(pos, world.base, start):
                base_threat = 5
            # Shared HP information makes all three clients favor a damaged enemy.
            focus = 2 if i != turret and hp <= 70 else 0
            ally_support = sum(sum(abs(a-b) for a,b in zip(ally, pos)) <= 5
                               for ally in world.ally_positions)
            return distance + shots - base_threat - focus - 0.5 * ally_support

        # Compete between the turret route and tank elimination, rather than only
        # rushing a turret while passing a nearly destroyed enemy.
        targets = sorted([turret] + tank_indices, key=target_priority)
        deadline = time.perf_counter() + self.rules.time_budget_ms / 1000
        action = None
        for target in targets:
            action = self._search(world, target, deadline)
            if action is not None or time.perf_counter() >= deadline:
                break
        if action is None:
            action = self.emergency(world, immediate)
            self.reason = "Legal fallback: survival, damage, cover, progress"
        self.recent.append(start.pos)
        return action

    def emergency(self, world: World, candidates=None) -> str:
        start = world.start
        turret = world.enemy_index.get(self.rules.target_symbol, 0)
        if candidates is None:
            candidates = list(world.successors(start, turret, root=True))
        visits = Counter(self.recent)
        goal = world.positions.get(self.rules.target_symbol, start.pos)

        def rank(item):
            command, node, cost = item
            damage = sum(a-b for a, b in zip(start.enemy_hp, node.enemy_hp))
            distance = sum(abs(a-b) for a,b in zip(node.pos, goal))
            return (node.hp > 0, node.hp, damage, -world.future_risk(node),
                    -visits[node.pos], -distance, -cost)

        # S incurs a yellow card in Team Battle. Emit it only if there is literally
        # no move, useful shot or valid supply command; no legal action then exists.
        return max(candidates, key=rank)[0] if candidates else "S"

    def _search(self, world: World, target: int, deadline: float) -> str | None:
        rules, start = self.rules, world.start
        goal = world.positions[world.enemy_names[target]]
        serial = itertools.count()
        visits = Counter(self.recent)

        # Relaxed navigation distances account for walls/water and the actual firing
        # cells. Trees/enemies are treated as traversable only for this lower bound.
        distances = {}
        pending = deque()
        for dr, dc in DIRS.values():
            for k in range(1, rules.shoot_range + 1):
                pos = goal[0] + dr*k, goal[1] + dc*k
                if not world.inside(pos):
                    break
                cell = world.cell(pos, start)
                if cell in ("R", "F") or (cell in world.positions and cell not in world.enemy_index):
                    break
                if cell == "W":
                    continue
                if (rules.team_mode and world.base and max(abs(pos[0]-world.base[0]),
                        abs(pos[1]-world.base[1])) <= rules.base_exclusion_radius):
                    continue
                distances[pos] = 0
                pending.append(pos)
        while pending:
            pos = pending.popleft()
            for dr, dc in DIRS.values():
                nxt = pos[0]+dr, pos[1]+dc
                if not world.inside(nxt) or nxt in distances:
                    continue
                cell = world.cell(nxt, start)
                if cell not in ("G", "S", "T") and cell not in world.enemy_index:
                    continue
                if (rules.team_mode and world.base and max(abs(nxt[0]-world.base[0]),
                        abs(nxt[1]-world.base[1])) <= rules.base_exclusion_radius):
                    continue
                distances[nxt] = distances[pos] + 1
                pending.append(nxt)

        if start.pos not in distances:
            return None

        def heuristic(node):
            distance = distances.get(node.pos, world.height * world.width)
            hp = node.enemy_hp[target]
            maximum = max(rules.normal_damage, rules.mega_damage)
            return distance + (hp + maximum - 1) // maximum

        queue = [(heuristic(start), 0.0, next(serial), start, "")]
        # Pareto labels retain HP and ammo tradeoffs instead of visiting each cell once.
        labels = {}

        def accept(node, cost):
            key = node.pos, node.cleared, node.enemy_hp
            entries = labels.setdefault(key, [])
            if any(c <= cost and h >= node.hp and n >= node.normal and m >= node.mega
                   for c, h, n, m in entries):
                return False
            entries[:] = [(c, h, n, m) for c, h, n, m in entries
                          if not (cost <= c and node.hp >= h and node.normal >= n and node.mega >= m)]
            entries.append((cost, node.hp, node.normal, node.mega))
            return True

        accept(start, 0.0)
        count = 0
        best_prefix = None
        best_prefix_rank = (float("inf"), float("inf"))
        while queue and count < rules.max_nodes and time.perf_counter() < deadline:
            _, cost, _, node, first = heapq.heappop(queue)
            key = node.pos, node.cleared, node.enemy_hp
            if (cost, node.hp, node.normal, node.mega) not in labels[key]:
                continue
            count += 1
            if node.enemy_hp[target] <= 0 or world.victory(node):
                self.reason = "Planned destruction of " + world.enemy_names[target]
                self.expanded = count
                return first
            for command, nxt, step_cost in world.successors(node, target, root=not first):
                if nxt.hp <= 0 and not world.victory(nxt):
                    continue
                total = cost + step_cost
                if not first and command.endswith(" A"):
                    # Separate teammates when possible. Slots 2/3 break equal-cost
                    # lane ties in opposite directions, without reserving occupied cells.
                    crowd = sum(sum(abs(a-b) for a,b in zip(nxt.pos, ally)) <= 1
                                for ally in world.ally_positions)
                    total += 0.06 * crowd
                    dr, dc = DIRS[command[0]]
                    side = (goal[0] - start.pos[0]) * dc - (goal[1] - start.pos[1]) * dr
                    preferred = -1 if rules.player_slot == 2 else 1 if rules.player_slot == 3 else 0
                    if side * preferred < 0:
                        total += 0.03
                if not first:
                    total += 0.08 * visits[nxt.pos]
                if accept(nxt, total):
                    remaining = heuristic(nxt)
                    rank = (remaining, total + remaining)
                    if rank < best_prefix_rank:
                        best_prefix_rank = rank
                        best_prefix = first or command
                    heapq.heappush(queue, (total + heuristic(nxt), total, next(serial),
                                           nxt, first or command))
        self.expanded = count
        if best_prefix:
            self.reason = "Bounded search: advance along best explored route"
        return best_prefix


class LocalBridge:
    """The public starter protocol: localhost:8747, INIT nickname, trailing space.

    Prefer the supplied official bridge when available. This fallback handles
    split reads and uses sendall; it does not change the game or send other data.
    """
    def __init__(self, host="127.0.0.1", port=8747):
        self.host, self.port = host, port
        self.sock = None

    def init(self, nickname):
        self.sock = socket.create_connection((self.host, self.port))
        return self.submit("INIT " + nickname)

    def submit(self, command):
        self.sock.sendall((command + " ").encode("utf-8"))
        buffer = bytearray()
        while len(buffer) < 1024 * 1024:
            part = self.sock.recv(65536)
            if not part:
                return None
            buffer.extend(part)
            try:
                raw = buffer.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if raw.lstrip().startswith(("0", "-")):
                return None
            if "\n" not in raw:
                continue
            try:
                parse_data(raw)
            except ValueError:
                continue  # Header/rows can cross TCP reads.
            if select.select([self.sock], [], [], 0.002)[0]:
                continue
            return raw
        raise ValueError("Unexpectedly large game packet")

    def close(self):
        if self.sock is not None:
            self.sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", type=Path, help="Read one captured game-data message")
    parser.add_argument("--rules", type=Path, help="JSON overrides for Rules")
    parser.add_argument("--nickname", default=NICKNAME)
    args = parser.parse_args()
    overrides = json.loads(args.rules.read_text(encoding="utf-8")) if args.rules else {}
    unknown = set(overrides) - {field.name for field in fields(Rules)}
    if unknown:
        parser.error("Unknown rule fields: " + ", ".join(sorted(unknown)))
    overrides.setdefault("player_slot", PLAYER_SLOT)
    bot = BattleBot(Rules(**overrides))
    if args.offline:
        print(bot.choose_action(parse_data(args.offline.read_text(encoding="utf-8"))))
        print(bot.reason, file=sys.stderr)
        return
    # Use official communication functions if distributed separately.
    try:
        from libs._bridge import init, submit, close
    except ImportError:
        bridge = LocalBridge()
        init, submit, close = bridge.init, bridge.submit, bridge.close
    try:
        data = init(args.nickname)
        while data:
            state = parse_data(data)
            try:
                command = bot.choose_action(state)
            except Exception as exc:
                # An algorithm exception must not end a team's connection.
                print(f"[RECOVERY] {type(exc).__name__}: {exc}", file=sys.stderr)
                command = bot.emergency(World(state, bot.rules))
            print(f"[ACTION] {command} ({bot.reason})", file=sys.stderr)
            data = submit(command)
    finally:
        close()


if __name__ == "__main__":
    main()
