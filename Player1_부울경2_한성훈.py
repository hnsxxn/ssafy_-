"""배틀싸피 팀배틀 봇. Python 3.12 / 외부 패키지 불필요.

기본은 battle.py의 M/socket 형식. main.py 환경은 --bridge 옵션.
확인되지 않은 규칙은 CONFIG와 사용안내를 먼저 확인하세요.
"""
import argparse
import heapq
import itertools
import socket
import sys
from collections import Counter, deque
from dataclasses import dataclass

NICKNAME = '부울경2_한성훈'
CONFIG = {
    'shot_range': 1,       # 첨부 자료에서 확인 가능한 인접 공격만 기본 사용
    'normal_damage': 30,
    'mega_damage': 70,
    'tree_hp': 0,          # 0: 나무 파괴 비활성. HELP 확인 후 실제 체력 설정
    'risk_weight': 0.035,
}
DIRS = (('U', -1, 0), ('R', 0, 1), ('D', 1, 0), ('L', 0, -1))


@dataclass
class State:
    grid: list
    allies: dict
    enemies: dict
    codes: list
    me: str

    @classmethod
    def parse(cls, text):
        lines = text.strip().splitlines()
        h, w, a, e, c = map(int, lines[0].split())
        if len(lines) < 1 + h + a + e + c:
            raise ValueError('불완전한 게임 데이터')
        grid = [line.split() for line in lines[1:1+h]]
        if not h or not w or any(len(row) != w for row in grid):
            raise ValueError('지도 크기 불일치')
        pos = 1+h
        def table(start, count):
            result = {}
            for line in lines[start:start+count]:
                key, *values = line.split()
                result[key] = values
            return result
        allies = table(pos, a)
        enemies = table(pos+a, e)
        # A/M 혼용 금지: 실제 입력의 자기 탱크 상세 필드로 식별한다.
        candidates = [k for k, v in allies.items() if len(v) >= 4]
        me = 'M' if 'M' in candidates else 'A' if 'A' in candidates else None
        if me is None:
            raise ValueError('자기 탱크 상세 정보 없음')
        return cls(grid, allies, enemies,
                   lines[pos+a+e:pos+a+e+c], me)

    def positions(self):
        return {tile: (r, c) for r, row in enumerate(self.grid)
                for c, tile in enumerate(row)
                if tile in self.allies or tile in self.enemies}

    def inside(self, p):
        return 0 <= p[0] < len(self.grid) and 0 <= p[1] < len(self.grid[0])

    def tile(self, p):
        return self.grid[p[0]][p[1]]


def distance(a, b):
    return abs(a[0]-b[0]) + abs(a[1]-b[1])


def hit(s, origin, direction, limit, ignore=None):
    """첫 장애물만 반환. 아군/나무 뒤의 적을 쏘지 않는다.

    물도 보수적으로 차폐물로 취급한다. HELP 확인 전 투과를 가정하지 않는다.
    """
    _, dr, dc = DIRS[direction]
    for step in range(1, limit+1):
        p = origin[0]+dr*step, origin[1]+dc*step
        if not s.inside(p):
            return None
        tile = s.tile(p)
        if tile == 'G' or tile == ignore:
            continue
        return tile, p
    return None


class Bot:
    def __init__(self, config=None):
        self.cfg = dict(CONFIG)
        if config:
            self.cfg.update(config)
        self.history = deque(maxlen=8)
        self.rejected_moves = set()
        self.last_move = None
        self.turn = 0

    def forbidden(self, s, p, positions):
        home = positions.get('H')
        return home is not None and max(abs(p[0]-home[0]), abs(p[1]-home[1])) <= 1

    def risk(self, s, positions, me):
        """확정 사선 + 적이 한 칸 이동해 만들 수 있는 사선의 위험도."""
        danger = Counter()
        for name in s.enemies:
            if name == 'X' or name not in positions:
                continue
            origin = positions[name]
            sources = [(origin, 1.0)]
            for _, dr, dc in DIRS:
                q = origin[0]+dr, origin[1]+dc
                if s.inside(q) and s.tile(q) == 'G':
                    sources.append((q, 0.25))
            for source, weight in sources:
                for _, dr, dc in DIRS:
                    for k in range(1, self.cfg['shot_range']+1):
                        p = source[0]+dr*k, source[1]+dc*k
                        if not s.inside(p):
                            break
                        tile = s.tile(p)
                        if tile not in ('G', s.me):
                            break
                        # 적 포탄 종류는 공개되지 않으므로 30은 위험 점수용 추정치.
                        danger[p] += 30*weight
        return danger

    def ammo_plan(self, hp, normal, mega):
        """남은 탄약으로 필요한 공격 수 최소화; 동률이면 메가 절약."""
        best = None
        nd, md = self.cfg['normal_damage'], self.cfg['mega_damage']
        for m in range(min(mega, (hp+md-1)//md)+1):
            n = max(0, (hp-m*md+nd-1)//nd)
            if n <= normal:
                candidate = (m+n, m, n)
                if best is None or candidate < best:
                    best = candidate
        return best

    def decide(self, s):
        self.turn += 1
        positions = s.positions()
        if s.me not in positions:
            raise ValueError('지도에서 자기 위치를 찾을 수 없음')
        start = positions[s.me]
        hp, _, normal, mega = s.allies[s.me][:4]
        hp, normal, mega = int(hp), int(normal), int(mega)
        if self.last_move:
            old, dest = self.last_move
            if start == old and s.inside(dest) and s.tile(dest) == 'G':
                self.rejected_moves.add((old, dest))
            self.last_move = None
        danger = self.risk(s, positions, start)
        self.history.append(start)
        repetitions = Counter(self.history)
        choices = []
        # Dijkstra: 탄약 사용량을 상태에 포함해 나무 돌파 후 탄약 부족 방지.
        # 양의 비용이므로 같은 위치/자원 상태의 순환은 채택하지 않는다.
        serial = itertools.count()
        initial = (start, normal, mega)
        pq = [(0.0, next(serial), initial, None, 0)]
        costs = {initial: 0.0}
        frontier = {}
        expanded = 0
        while pq:
            cost, _, state, first, steps = heapq.heappop(pq)
            if cost != costs.get(state):
                continue
            p, n, m = state
            expanded += 1
            # 16x16 전장에서도 과도한 자원 상태 확장을 제한한다.
            if expanded > 12000:
                break
            for d, (direction, _, _) in enumerate(DIRS):
                visible = hit(s, p, d, self.cfg['shot_range'], s.me)
                if not visible or visible[0] not in s.enemies:
                    continue
                target, target_pos = visible
                target_hp = int(s.enemies[target][0])
                plan = self.ammo_plan(target_hp, n, m)
                if plan is None:
                    # 탄약이 처치에는 모자라도 유효 피해로 팀의 격파를 돕는다.
                    if steps == 0 and (n or m):
                        use_mega = m > 0 and (n == 0 or target_hp > self.cfg['normal_damage'])
                        damage = min(target_hp, self.cfg['mega_damage'] if use_mega
                                     else self.cfg['normal_damage'])
                        command = direction+' '+('F M' if use_mega else 'F')
                        choices.append((-damage/10.0, 0, command, target))
                    continue
                attacks, use_m, use_n = plan
                # 포탑 격파, 두 번째 탱크 격파, 체력이 낮은 적 집중 순서.
                tank_count = sum(k != 'X' for k in s.enemies)
                bonus = 5.5 if target == 'X' else 4.5
                if target != 'X' and tank_count <= 2:
                    bonus += 2.0
                if target != 'X' and 'H' in positions:
                    if distance(target_pos, positions['H']) <= self.cfg['shot_range']+2:
                        bonus += 3.5
                if attacks == 1:
                    bonus += 2.5
                # 이미 사선에 있는 경우 킬/피해를 먼저 확보. 불필요한 후퇴 방지.
                if steps == 0:
                    bonus += 2.0
                exposure = danger[p] * min(attacks, 3) * self.cfg['risk_weight']
                if hp <= danger[p] and attacks > 1:
                    exposure += 5.0
                score = cost + attacks + exposure - bonus
                command = first or f'{direction} ' + ('F M' if use_m else 'F')
                choices.append((score, cost, command, target))
            for direction, dr, dc in DIRS:
                q = p[0]+dr, p[1]+dc
                if not s.inside(q) or self.forbidden(s, q, positions):
                    continue
                if (p, q) in self.rejected_moves:
                    continue
                tile = s.tile(q)
                options = []
                if tile in ('G', s.me):
                    options.append((n, m, 1, f'{direction} A'))
                elif tile == 'T' and self.cfg['tree_hp'] > 0:
                    plan = self.ammo_plan(self.cfg['tree_hp'], n, m)
                    if plan:
                        count, spend_m, spend_n = plan
                        options.append((n-spend_n, m-spend_m, count+1,
                                        f'{direction} ' + ('F M' if spend_m else 'F')))
                for nn, mm, turns, action in options:
                    weight = self.cfg['risk_weight'] * (1.8 if hp <= 30 else 1.0)
                    # 직전 칸으로 되돌아가는 반복을 줄이되 탈출 자체는 허용.
                    cycle = repetitions[q]*0.18 if first is None else 0.0
                    nc = cost + turns + danger[q]*weight + cycle
                    ns = (q, nn, mm)
                    if nc >= costs.get(ns, float('inf')):
                        continue
                    labels = frontier.setdefault(q, [])
                    if any(c <= nc and an >= nn and am >= mm for c, an, am in labels):
                        continue
                    labels[:] = [(c, an, am) for c, an, am in labels
                                 if not (nc <= c and nn >= an and mm >= am)]
                    labels.append((nc, nn, mm))
                    costs[ns] = nc
                    heapq.heappush(pq, (nc, next(serial), ns, first or action, steps+turns))
        if choices:
            command = min(choices)[2]
        else:
            # 현재 공격 가능한 목표가 없어도 정지하지 않고 합법적 이동을 선택.
            goals = [positions[k] for k in s.enemies if k in positions]
            fallback = []
            for direction, dr, dc in DIRS:
                q = start[0]+dr, start[1]+dc
                if (s.inside(q) and s.tile(q) == 'G'
                        and not self.forbidden(s, q, positions)
                        and (start, q) not in self.rejected_moves):
                    near = min((distance(q, g) for g in goals), default=0)
                    fallback.append((near + danger[q]*0.08 + repetitions[q]*2,
                                     direction+' A'))
            # 길이 완전히 막혀도 유효한 나무 공격이 있으면 정지하지 않는다.
            if not fallback and self.cfg['tree_hp'] > 0 and (normal or mega):
                for d, (direction, _, _) in enumerate(DIRS):
                    obstacle = hit(s, start, d, self.cfg['shot_range'], s.me)
                    if obstacle and obstacle[0] == 'T':
                        fallback.append((1, direction+' '+('F' if normal else 'F M')))
            command = min(fallback)[1] if fallback else 'S'
            if command == 'S':
                print('[WARN] 유효 이동/공격 없음. 정지 페널티 가능: 지도/규칙 확인', file=sys.stderr)
        if command.endswith(' A'):
            dr, dc = next((dr, dc) for d, dr, dc in DIRS if d == command[0])
            self.last_move = (start, (start[0]+dr, start[1]+dc))
        return command


class SocketBridge:
    """원본 battle.py의 INIT/submit 프로토콜. TCP 분할 수신을 누적한다."""
    def __init__(self, host, port, prefix=''):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(None)  # 상대 턴 대기를 임의 시간초과로 종료하지 않는다.
        self.prefix = prefix

    def submit(self, command):
        self.sock.sendall((self.prefix+command+' ').encode('utf-8'))
        data = bytearray()
        while True:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            data.extend(chunk)
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                continue
            if not text[:1].isdigit() or text[:1] == '0':
                return None
            lines = text.strip().splitlines()
            try:
                h, w, a, e, c = map(int, lines[0].split())
            except ValueError:
                continue
            needed = 1+h+a+e+c
            if len(lines) < needed:
                continue
            # 현재 원본 프로토콜은 길이 프레임이 없다. 마지막 줄의 종결
            # 개행이 없으면 짧게 후속 조각을 확인해 숫자/암호 중간 절단 방지.
            if not text.endswith('\n'):
                import select
                if select.select([self.sock], [], [], 0.02)[0]:
                    continue
            try:
                State.parse(text)
            except (ValueError, IndexError):
                continue
            return text

    def close(self):
        self.sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('prefix', nargs='?', default='', help='원본 실행기 ARGS')
    parser.add_argument('--bridge', action='store_true', help='main.py의 libs._bridge 사용')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8747)
    parser.add_argument('--range', type=int, default=CONFIG['shot_range'], dest='shot_range')
    parser.add_argument('--tree-hp', type=int, default=CONFIG['tree_hp'])
    args = parser.parse_args()
    if args.shot_range < 1 or args.tree_hp < 0:
        parser.error('사거리 >= 1, 나무 체력 >= 0 필요')
    bot = Bot({'shot_range': args.shot_range, 'tree_hp': args.tree_hp})
    bridge = None
    try:
        if args.bridge:
            from libs import _bridge as bridge
            data = bridge.init(NICKNAME)
        else:
            bridge = SocketBridge(args.host, args.port, args.prefix)
            data = bridge.submit('INIT '+NICKNAME)
        while data is not None:
            state = State.parse(data)
            command = bot.decide(state)
            print(f'[TURN {bot.turn}] {command}', flush=True)
            data = bridge.submit(command)
    except (OSError, ValueError, IndexError, ImportError) as exc:
        # 오류를 숨기고 무효 명령을 반복하지 않는다.
        print(f'[ERROR] {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    finally:
        if bridge is not None:
            bridge.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
