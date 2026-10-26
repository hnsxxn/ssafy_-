"""게임 엔진 대체물이 아닌 의사결정/통신 회귀 검사."""
import importlib.util
import pathlib
import random
import socket
import sys
import threading
import time
import unittest

PATH = pathlib.Path(__file__).with_name('Player1_부울경2_한성훈.py')
spec = importlib.util.spec_from_file_location('battle_strategy', PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def make(rows, hp=100, normal=99, mega=0, enemy_hp=None, me='M'):
    grid = [r.split() for r in rows]
    tiles = {x for row in grid for x in row}
    allies = {me: [str(hp), 'R', str(normal), str(mega)]}
    for key in ('H', 'B', 'C'):
        if key in tiles:
            allies[key] = ['100']
    enemies = {k: [str((enemy_hp or {}).get(k, 100))]
               for k in tiles if k == 'X' or k.startswith('E')}
    return mod.State(grid, allies, enemies, [], me)


def legal(s, command, cfg):
    pos = s.positions()
    p = pos[s.me]
    if command == 'S':
        return False
    tokens = command.split()
    if len(tokens) < 2:
        return False
    d = next((i for i, x in enumerate(mod.DIRS) if x[0] == tokens[0]), None)
    if d is None:
        return False
    _, dr, dc = mod.DIRS[d]
    if tokens[1] == 'A':
        q = p[0]+dr, p[1]+dc
        h = pos.get('H')
        return (s.inside(q) and s.tile(q) == 'G' and
                (h is None or max(abs(h[0]-q[0]), abs(h[1]-q[1])) > 1))
    if tokens[1] != 'F':
        return False
    ammo = int(s.allies[s.me][3 if len(tokens) > 2 else 2])
    target = mod.hit(s, p, d, cfg['shot_range'], s.me)
    return ammo > 0 and target is not None and (
        target[0] in s.enemies or (target[0] == 'T' and cfg['tree_hp'] > 0))


class StrategyTests(unittest.TestCase):
    def test_tower_finish(self):
        s = make(['M X'], enemy_hp={'X': 30})
        self.assertEqual(mod.Bot().decide(s), 'R F')

    def test_mega_finish(self):
        s = make(['M E1 G X'], normal=0, mega=1, enemy_hp={'E1': 70})
        self.assertEqual(mod.Bot().decide(s), 'R F M')

    def test_normal_finish_saves_mega(self):
        s = make(['M E1'], normal=1, mega=1, enemy_hp={'E1': 30})
        self.assertEqual(mod.Bot().decide(s), 'R F')

    def test_combo(self):
        bot = mod.Bot()
        self.assertEqual(bot.ammo_plan(100, 1, 1), (2, 1, 1))
        self.assertIsNone(bot.ammo_plan(100, 0, 1))

    def test_never_fire_through_ally(self):
        s = make(['G G G G', 'M B X G', 'G G G G'])
        bot = mod.Bot({'shot_range': 3})
        action = bot.decide(s)
        self.assertNotEqual(action, 'R F')
        self.assertTrue(legal(s, action, bot.cfg))

    def test_never_fire_through_rock(self):
        s = make(['G G G G', 'M R X G', 'G G G G'])
        bot = mod.Bot({'shot_range': 3})
        self.assertNotEqual(bot.decide(s), 'R F')

    def test_forbidden_home_square(self):
        s = make(['G G G G G', 'G H G G G', 'G G G M X', 'G G G G G'])
        bot = mod.Bot()
        for q in ((0, 0), (1, 2), (2, 2)):
            self.assertTrue(bot.forbidden(s, q, s.positions()))
        self.assertFalse(bot.forbidden(s, (2, 3), s.positions()))
        self.assertTrue(legal(s, bot.decide(s), bot.cfg))

    def test_no_ammo_move(self):
        s = make(['M G X', 'G G G'], normal=0)
        bot = mod.Bot()
        self.assertTrue(bot.decide(s).endswith(' A'))

    def test_long_shot_only_when_enabled(self):
        s = make(['M G G X'], enemy_hp={'X': 30})
        self.assertEqual(mod.Bot().decide(s), 'R A')
        self.assertEqual(mod.Bot({'shot_range': 3}).decide(s), 'R F')

    def test_tree_config(self):
        s = make(['M T G X'], normal=5, enemy_hp={'X': 30})
        bot = mod.Bot({'tree_hp': 30})
        self.assertEqual(bot.decide(s), 'R F')

    def test_tree_insufficient_ammo(self):
        s = make(['M T G X', 'G R R R'], normal=1)
        bot = mod.Bot({'tree_hp': 30})
        self.assertEqual(bot.decide(s), 'D A')

    def test_updated_grid_changes_route(self):
        bot = mod.Bot()
        first = make(['M G G X', 'G G G G'])
        self.assertEqual(bot.decide(first), 'R A')
        changed = make(['G M B X', 'G G G G'])
        self.assertEqual(bot.decide(changed), 'D A')

    def test_rejected_move_not_repeated(self):
        bot = mod.Bot()
        s = make(['M G G X', 'G G G G'])
        self.assertEqual(bot.decide(s), 'R A')
        self.assertEqual(bot.decide(s), 'D A')

    def test_parse_a_and_m(self):
        for name in ('A', 'M'):
            text = f'1 3 1 1 1\n{name} G X\n{name} 100 R 99 2\nX 100\nABCD\n'
            state = mod.State.parse(text)
            self.assertEqual(state.me, name)
            self.assertEqual(state.codes, ['ABCD'])

    def test_arbitrary_whitespace(self):
        text = '1  2  1 1 0\r\nM   X\r\nM 100 R 99 0\r\nX 30\r\n'
        self.assertEqual(mod.Bot().decide(mod.State.parse(text)), 'R F')

    def test_partial_packet(self):
        a, b = socket.socketpair()
        bridge = object.__new__(mod.SocketBridge)
        bridge.sock, bridge.prefix = a, ''
        def send():
            b.recv(1024)
            for part in (b'1 ', b'2 1 1 0\nM X\nM 100 R 99 0\nX ', b'100\n'):
                b.sendall(part)
                time.sleep(0.003)
        worker = threading.Thread(target=send)
        worker.start()
        try:
            state = mod.State.parse(bridge.submit('INIT TEST'))
            self.assertEqual(state.enemies['X'], ['100'])
        finally:
            worker.join()
            a.close()
            b.close()

    def test_random_legal_actions(self):
        rng = random.Random(20261026)
        for iteration in range(400):
            size = 7
            rows = [[rng.choices(['G', 'R', 'W', 'F', 'T'], [80, 5, 5, 5, 5])[0]
                     for _ in range(size)] for _ in range(size)]
            cells = rng.sample([(r, c) for r in range(size) for c in range(size)], 7)
            for tile, (r, c) in zip(('M', 'X', 'H', 'B', 'E1', 'E2', 'E3'), cells):
                rows[r][c] = tile
            s = make([' '.join(r) for r in rows], normal=rng.randint(0, 8),
                     mega=rng.randint(0, 3))
            bot = mod.Bot({'shot_range': rng.choice([1, 3]),
                           'tree_hp': rng.choice([0, 30])})
            action = bot.decide(s)
            if action != 'S':
                self.assertTrue(legal(s, action, bot.cfg), (iteration, action, rows))
            else:
                p = s.positions()['M']
                for direction, _, _ in mod.DIRS:
                    self.assertFalse(legal(s, direction+' A', bot.cfg))
                    for suffix in ('F', 'F M'):
                        self.assertFalse(legal(s, direction+' '+suffix, bot.cfg))


if __name__ == '__main__':
    unittest.main(verbosity=2)
