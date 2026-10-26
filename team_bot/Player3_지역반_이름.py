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

NICKNAME = "지역반_이름3"  # 제출 전 본인 닉네임으로 수정 (12자 이내)
PLAYER_SLOT = 3  # 1: 포탑 공격/회피, 2/3: 포탑 방어/적 섬멸
BOT_VERSION = "2.0"

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
    role: str = "auto"  # auto: slot 1 attacker, slots 2/3 defenders
    defense_leash: int = 6  # Chebyshev distance from our base
    threat_radius: int = 8  # Manhattan distance of an approaching enemy
    sand_cost: float = 50.0
    hp_cost: float = 0.20
    next_move_risk_cost: float = 1.5

    def __post_init__(self):
        for key in ("shoot_range", "normal_damage", "mega_damage", "supply_gain",
                    "mega_capacity", "max_nodes", "time_budget_ms"):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")
        if self.role not in ("auto", "attacker", "defender"):
            raise ValueError("role must be auto, attacker or defender")
        if self.defense_leash < 2 or self.threat_radius < 1 or self.sand_cost < 0:
            raise ValueError("Invalid defense/sand configuration")
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
    def __init__(self, state: dict, rules: Rules, known_terrain=None):
        self.rules = rules
        self.state = state
        self.known_terrain = known_terrain or {}
        self.allow_sand = False
        self.role = rules.role if rules.role != "auto" else ("attacker" if rules.player_slot == 1 else "defender")
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
        self.base_hp = int(state["allies"].get(rules.base_symbol, [100])[0])
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
            return self.known_terrain.get(pos, "G")
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
            if pos == node.pos and source != node.pos and target != node.pos:
                return False  # Our projected tank can shield the base.
            if self.cell(pos, node) not in ("G", "W", "S"):
                return False
        return True

    def adjacent_supply(self, pos: Pos) -> bool:
        return any(self.inside((pos[0] + dr, pos[1] + dc))
                   and self.grid[pos[0] + dr][pos[1] + dc] == "F" for dr, dc in DIRS.values())

    def movable(self, pos: Pos, node: Node) -> bool:
        if not self.inside(pos) or self.cell(pos, node) not in ("G", "S"):
            return False
        if not self.allow_sand and self.cell(pos, node) == "S":
            return False
        if self.rules.team_mode and self.base and self.role == "defender":
            current = max(abs(node.pos[0]-self.base[0]), abs(node.pos[1]-self.base[1]))
            destination = max(abs(pos[0]-self.base[0]), abs(pos[1]-self.base[1]))
            if current <= self.rules.defense_leash and destination > self.rules.defense_leash:
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

    def incoming_base(self, node: Node) -> int:
        if self.base is None:
            return 0
        return self.rules.enemy_damage * sum(
            node.enemy_hp[i] > 0 and name != self.rules.target_symbol
            and self.sees(self.positions[name], self.base, node)
            for i,name in enumerate(self.enemy_names))

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
            if terrain_damage:
                cost += rules.sand_cost
            cost += self.future_risk(nxt) * rules.next_move_risk_cost
            if self.role == "defender" and self.base and not won:
                base_danger = self.incoming_base(nxt)
                cost += base_danger * 0.2
                if base_danger >= self.base_hp:
                    cost += 1000
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


def check_action(state: dict, command: str, rules: Rules, allow_sand=True) -> bool:
    """Final gate against the RAW current snapshot, never projected search data."""
    if not isinstance(command, str):
        return False
    grid, allies, enemies = state["grid"], state["allies"], state["enemies"]
    own = rules.own_symbol
    if own is None:
        detailed = [k for k,v in allies.items() if len(v) >= 4 and str(v[1]) in DIRS]
        if len(detailed) != 1:
            return False
        own = detailed[0]
    pos = next(((r,c) for r,row in enumerate(grid) for c,x in enumerate(row) if x == own), None)
    if pos is None or own not in allies:
        return False
    stats = allies[own]
    parts = command.split()
    if len(parts) < 2 or parts[0] not in DIRS:
        if len(parts) != 2 or parts[0] != "G":
            return False
        if rules.caesar_shift is None or not state.get("codes"):
            return False
        if int(stats[3]) >= rules.mega_capacity:
            return False
        if parts[1] != decode_caesar(state["codes"][0], rules.caesar_shift):
            return False
        return any(0 <= pos[0]+dr < len(grid) and 0 <= pos[1]+dc < len(grid[0])
                   and grid[pos[0]+dr][pos[1]+dc] == "F" for dr,dc in DIRS.values())
    direction = parts.pop(0)
    dr,dc = DIRS[direction]
    def inside(r,c):
        return 0 <= r < len(grid) and 0 <= c < len(grid[0])
    if parts == ["A"]:
        r,c = pos[0]+dr,pos[1]+dc
        if not inside(r,c) or grid[r][c] not in (("G","S") if allow_sand else ("G",)):
            return False
        base = next(((r,c) for r,row in enumerate(grid) for c,x in enumerate(row) if x == rules.base_symbol), None)
        return not (rules.team_mode and base and max(abs(r-base[0]),abs(c-base[1])) <= rules.base_exclusion_radius)
    if parts not in (["F"], rules.mega_command.split()):
        return False
    if int(stats[3 if len(parts) > 1 else 2]) <= 0:
        return False
    for k in range(1, rules.shoot_range+1):
        r,c = pos[0]+dr*k,pos[1]+dc*k
        if not inside(r,c):
            return False
        cell = grid[r][c]
        if cell in ("G","S","W"):
            continue
        # Dead entries, allies, rocks, facilities and unknown symbols block a shot.
        return cell == "T" or (cell in enemies and int(enemies[cell][0]) > 0)
    return False


class BattleBot:
    def __init__(self, rules: Rules | None = None):
        self.rules = rules or Rules()
        self.recent = deque(maxlen=12)
        self.reason = ""
        self.expanded = 0
        self.turn = 0
        self.terrain = {}
        self.last_shot = None
        self.blocked_shots = {}
        self.feedback = ""
        self.action_detail = ""
        self._sand_required = False

    def _observe(self, state):
        self.turn += 1
        self.feedback = ""
        for r,row in enumerate(state["grid"]):
            for c,cell in enumerate(row):
                if cell in ("G","S"):
                    self.terrain[r,c] = cell
        self.blocked_shots = {k:v for k,v in self.blocked_shots.items() if v >= self.turn}
        if self.last_shot:
            name, before, origin, direction, target_pos = self.last_shot
            current = state["enemies"].get(name)
            if current is not None and int(current[0]) >= before:
                # This is evidence of no observed damage, not proof of the cause.
                self.blocked_shots[origin,direction,name,target_pos] = self.turn + 2
                self.feedback = "Previous shot caused no observed HP drop; same firing position temporarily blocked"
            self.last_shot = None

    def _shot_key(self, world, command):
        parts = command.split()
        if len(parts) < 2 or parts[0] not in DIRS or parts[1] != "F":
            return None
        hit = world.ray(world.start.pos, parts[0], world.start)
        if hit and hit[1] in world.enemy_index:
            return world.start.pos, parts[0], hit[1], hit[0]
        return None

    def _allowed(self, world, command):
        if not check_action(world.state, command, self.rules, self._sand_required):
            return False
        key = self._shot_key(world, command)
        return key is None or key not in self.blocked_shots

    def _finish(self, world, command):
        if not self._allowed(world, command):
            self.reason = "Final snapshot gate replaced a stale/invalid/repeated action"
            command = self.emergency(world)
        # This second independent gate applies to all paths, including recovery.
        if command != "S" and not self._allowed(world, command):
            command = "S"
        key = self._shot_key(world, command)
        self.action_detail = ""
        if key:
            origin, direction, name, target_pos = key
            distance = abs(origin[0]-target_pos[0])+abs(origin[1]-target_pos[1])
            self.action_detail = f"target={name}@{target_pos} distance={distance}"
            self.last_shot = name, world.start.enemy_hp[world.enemy_index[name]], origin, direction, target_pos
        if command.endswith(" A"):
            dr,dc = DIRS[command[0]]
            pos = world.start.pos[0]+dr,world.start.pos[1]+dc
            self.terrain[pos] = world.grid[pos[0]][pos[1]]
            self.action_detail = f"destination={pos} terrain={self.terrain[pos]} sand_required={self._sand_required}"
        elif " F" in command and not key:
            hit = world.ray(world.start.pos, command[0], world.start)
            self.action_detail = f"obstacle={hit}"
        self.recent.append(world.start.pos)
        return command

    def choose_action(self, state: dict) -> str:
        self._observe(state)
        self._sand_required = False
        world = World(state, self.rules, self.terrain)
        if world.start.hp <= 0:
            return "S"
        # Plan on dry ground first, regardless of the length of a sand shortcut.
        deadline = time.perf_counter() + self.rules.time_budget_ms/1000
        command = self._decide(world, deadline)
        return self._finish(world, command)

    def _candidates(self, world):
        turret = world.enemy_index.get(self.rules.target_symbol, 0)
        return [item for item in world.successors(world.start, turret, True)
                if self._allowed(world, item[0])]

    def _decide(self, world, deadline):
        immediate = self._candidates(world)
        wins = [item for item in immediate if world.victory(item[1]) and " F" in item[0]]
        if wins:
            self.reason = "Immediate team victory"
            return min(wins,key=lambda x:(world.start.mega-x[1].mega,x[2]))[0]
        if world.role == "defender" and world.base:
            command = self._defend(world, immediate, deadline)
        else:
            command = self._attack(world, immediate, deadline)
        if command:
            return command
        # A sand route is considered only after no dry objective route was found.
        if world.role == "attacker":
            turret = world.enemy_index.get(self.rules.target_symbol)
            if turret is not None and time.perf_counter() < deadline:
                world.allow_sand = True
                self._sand_required = True
                command = self._search(world, turret, deadline)
                if command and self._allowed(world, command):
                    self.reason += "; no dry objective route, sand permitted"
                    return command
                world.allow_sand = False
                self._sand_required = False
        return self.emergency(world)

    def _safe_shots(self, world, candidates, targets):
        result=[]
        for item in candidates:
            key=self._shot_key(world,item[0])
            if key and key[2] in targets and (item[1].hp > 0 or world.victory(item[1])):
                result.append(item)
        return result

    def _best_shot(self, world, shots, threat_rank=None):
        def rank(item):
            key=self._shot_key(world,item[0])
            index=world.enemy_index[key[2]]
            killed=item[1].enemy_hp[index] <= 0
            damage=world.start.enemy_hp[index]-item[1].enemy_hp[index]
            urgency=threat_rank(key[2]) if threat_rank else 0
            return urgency, killed, item[1].hp, damage, -(world.start.mega-item[1].mega)
        return max(shots,key=rank)[0]

    def _supply(self, world, candidates):
        if world.start.mega < 2 and world.incoming(world.start) == 0:
            for command,node,_ in candidates:
                if command.startswith("G ") and node.hp > 0:
                    self.reason="Safe ammunition supply"
                    return command
        return None

    def _attack(self, world, candidates, deadline):
        turret=world.enemy_index.get(self.rules.target_symbol)
        # Preserve the attacker's objective: a safe turret shot precedes a nonwinning
        # tank kill, and the attacker never pursues a remote tank.
        shots=self._safe_shots(world,candidates,{self.rules.target_symbol})
        clear=[x for x in shots if x[1].hp == world.start.hp]
        if clear:
            self.reason="Attacker: safe turret damage"
            return self._best_shot(world,clear)
        supply=self._supply(world,candidates)
        if supply:
            return supply
        if turret is not None:
            command=self._search(world,turret,deadline)
            if command:
                self.reason="Attacker: turret route with evasion"
                return command
        # A tank may be shot to remove an immediate firing threat, never chased.
        threats={name for i,name in enumerate(world.enemy_names)
                 if name != self.rules.target_symbol and world.start.enemy_hp[i] > 0
                 and world.sees(world.positions[name],world.start.pos,world.start)}
        shots=self._safe_shots(world,candidates,threats)
        if shots:
            self.reason="Attacker: eliminate a blocking firing threat"
            return self._best_shot(world,shots)
        return None

    def _defend(self, world, candidates, deadline):
        base=world.base
        def distance(pos):
            return abs(pos[0]-base[0])+abs(pos[1]-base[1])
        threats={name for i,name in enumerate(world.enemy_names)
                 if name != self.rules.target_symbol and world.start.enemy_hp[i] > 0
                 and (distance(world.positions[name]) <= self.rules.threat_radius
                      or world.sees(world.positions[name],base,world.start))}
        def urgency(name):
            return (100 if world.sees(world.positions[name],base,world.start) else 0) - distance(world.positions[name])
        if world.incoming_base(world.start) >= world.base_hp:
            protects=[item for item in candidates if item[1].hp > 0
                      and world.incoming_base(item[1]) < world.base_hp]
            if not protects:
                # A small sand loss is justified if it is the only way to stop an
                # immediately lethal shot at our base.
                world.allow_sand=True
                self._sand_required=True
                protects=[item for item in self._candidates(world) if item[1].hp > 0
                          and world.incoming_base(item[1]) < world.base_hp]
                if not protects:
                    world.allow_sand=False
                    self._sand_required=False
            if protects:
                def protection_rank(item):
                    _,node,cost=item
                    damage=sum(a-b for a,b in zip(world.start.enemy_hp,node.enemy_hp))
                    return (-world.incoming_base(node),node.hp,damage,-cost)
                self.reason="Defender: prevent a lethal base volley"
                return max(protects,key=protection_rank)[0]
        shots=self._safe_shots(world,candidates,threats)
        if shots:
            self.reason="Defender: intercept an approaching enemy"
            return self._best_shot(world,shots,urgency)
        # Stay in the guard area. Only enemies already threatening our base warrant
        # a kill plan; its movement is still restricted by the defense leash.
        for name in sorted(threats,key=urgency,reverse=True):
            command=self._search(world,world.enemy_index[name],min(deadline,time.perf_counter()+0.045))
            if command:
                self.reason="Defender: intercept within base guard area"
                return command
        if not threats:
            supply=self._supply(world,candidates)
            if supply:
                return supply
        goals=self._guard_cells(world)
        if goals and time.perf_counter() < deadline:
            command=self._search(world,world.enemy_index.get(self.rules.target_symbol,0),deadline,goals=goals)
            if command:
                self.reason="Defender: patrol assigned base flank"
                return command
        return None

    def _guard_cells(self, world):
        base=world.base
        target=world.positions.get(self.rules.target_symbol,(0,world.width-1))
        forward=target[0]-base[0],target[1]-base[1]
        side=1 if self.rules.player_slot == 2 else -1
        cells=[]
        for r in range(max(0,base[0]-4),min(world.height,base[0]+5)):
            for c in range(max(0,base[1]-4),min(world.width,base[1]+5)):
                pos=(r,c)
                dr,dc=r-base[0],c-base[1]
                radius=max(abs(dr),abs(dc))
                if radius < 2 or radius > 4 or pos == world.start.pos or not world.movable(pos,world.start):
                    continue
                facing=forward[0]*dr+forward[1]*dc
                flank=side*(forward[0]*dc-forward[1]*dr)
                if facing < 0:
                    continue
                # Prefer separate flanks and a short guard ring, then spread away
                # from the other defender instead of competing for one tile.
                crowded=sum(abs(r-a[0])+abs(c-a[1]) <= 1 for a in world.ally_positions)
                score=(flank > 0, -crowded, -abs(radius-3), -Counter(self.recent)[pos])
                cells.append((score,pos))
        if not cells:
            return set()
        best=max(score for score,_ in cells)
        return {pos for score,pos in cells if score == best}

    def emergency(self, world: World, candidates=None) -> str:
        # Called independently by main() as well, so it rechecks the raw snapshot.
        dry=self._candidates(world) if candidates is None else [x for x in candidates if self._allowed(world,x[0])]
        safe=[x for x in dry if x[1].hp > 0]
        choices=safe or dry
        if not choices:
            # Sand is also allowed to escape a boxed-in position, but not a lethal
            # sand step when an alternative useful shot/move exists.
            world.allow_sand=True
            self._sand_required=True
            choices=self._candidates(world)
        if not choices:
            self.reason="No legal move, useful shot or supply exists"
            return "S"
        start=world.start
        visits=Counter(self.recent)
        goal=(world.base if world.role == "defender" and world.base
              else world.positions.get(self.rules.target_symbol,start.pos))
        def rank(item):
            command,node,cost=item
            damage=sum(a-b for a,b in zip(start.enemy_hp,node.enemy_hp))
            progress=-(abs(node.pos[0]-goal[0])+abs(node.pos[1]-goal[1]))
            # Don't clear unrelated trees while a safe dry patrol move is possible.
            key=self._shot_key(world,command)
            useful_fire=damage if key else 0
            return (node.hp > 0,node.hp,useful_fire,-world.future_risk(node),
                    -visits[node.pos],progress,command.endswith(" A"),-cost)
        self.reason="Checked fallback: survival and role position"
        return max(choices,key=rank)[0]

    def _search(self, world: World, target: int, deadline: float, goals=None) -> str | None:
        rules, start = self.rules, world.start
        goal = world.positions[world.enemy_names[target]]
        serial = itertools.count()
        visits = Counter(self.recent)

        # Relaxed navigation distances account for walls/water and the actual firing
        # cells. Trees/enemies are treated as traversable only for this lower bound.
        distances = {}
        pending = deque()
        if goals is not None:
            for pos in goals:
                distances[pos]=0
                pending.append(pos)
        else:
            for dr, dc in DIRS.values():
                for k in range(1, rules.shoot_range + 1):
                    pos = goal[0] + dr*k, goal[1] + dc*k
                    if not world.inside(pos):
                        break
                    cell = world.cell(pos, start)
                    if cell in ("R", "F") or (cell in world.positions and cell not in world.enemy_index):
                        break
                    if cell == "W" or (cell == "S" and not world.allow_sand):
                        continue
                    if (rules.team_mode and world.base and max(abs(pos[0]-world.base[0]),
                            abs(pos[1]-world.base[1])) <= rules.base_exclusion_radius):
                        continue
                    if (world.role == "defender" and world.base
                            and max(abs(pos[0]-world.base[0]),abs(pos[1]-world.base[1])) > rules.defense_leash):
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
                if cell == "S" and not world.allow_sand:
                    continue
                if (world.role == "defender" and world.base
                        and max(abs(start.pos[0]-world.base[0]),abs(start.pos[1]-world.base[1])) <= rules.defense_leash
                        and max(abs(nxt[0]-world.base[0]),abs(nxt[1]-world.base[1])) > rules.defense_leash):
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
            if goals is not None:
                return distance
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
            if ((goals is not None and node.pos in goals)
                    or (goals is None and node.enemy_hp[target] <= 0)
                    or world.victory(node)):
                self.reason = "Planned destruction of " + world.enemy_names[target]
                self.expanded = count
                return first
            for command, nxt, step_cost in world.successors(node, target, root=not first):
                if not first and not self._allowed(world, command):
                    continue
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
                    total += 0.5 * visits[nxt.pos]
                if accept(nxt, total):
                    remaining = heuristic(nxt)
                    rank = (total + remaining, remaining)
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
    import platform
    print(f"[RUNTIME] {platform.python_implementation()} {platform.python_version()} / slot={PLAYER_SLOT} / v{BOT_VERSION}", file=sys.stderr)
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
                world = World(state, bot.rules, bot.terrain)
                command = bot._finish(world, bot.emergency(world))
            if bot.feedback:
                print(f"[VERIFY] {bot.feedback}", file=sys.stderr)
            print(f"[ACTION v{BOT_VERSION} slot={PLAYER_SLOT}] {command} ({bot.reason}) {bot.action_detail}", file=sys.stderr)
            data = submit(command)
    finally:
        close()


if __name__ == "__main__":
    main()
