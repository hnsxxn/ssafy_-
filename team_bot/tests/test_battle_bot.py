import copy
import random
import time
import unittest
from unittest.mock import patch

from battle_bot import BattleBot, Rules, World, Node, parse_data, decode_caesar, LocalBridge

DIRECTIONS = {'U': (-1, 0), 'R': (0, 1), 'D': (1, 0), 'L': (0, -1)}


def state(rows, *, hp=100, normal=99, mega=0, target_hp=100, enemy_hp=None, codes=None):
    grid = [row.split() if isinstance(row, str) else row[:] for row in rows]
    units = {cell for row in grid for cell in row}
    enemies = {cell: [target_hp if cell == 'X' else (enemy_hp or {}).get(cell, 100)]
               for cell in units if cell == 'X' or cell.startswith('E')}
    allies = {'M': [hp, 'R', normal, mega]}
    allies.update({cell: [100] for cell in units if cell == 'H' or cell.startswith('M') and cell != 'M'})
    return {'grid': grid, 'allies': allies, 'enemies': enemies, 'codes': codes or []}


def legal(s, command, rules):
    """Independent command referee, using the supplied manual/slides."""
    grid = s['grid']
    pos = next((r,c) for r,row in enumerate(grid) for c,x in enumerate(row) if x == 'M')
    base = next(((r,c) for r,row in enumerate(grid) for c,x in enumerate(row) if x == 'H'), None)
    stats = s['allies']['M']
    parts = command.split()
    if parts and parts[0] in DIRECTIONS:
        direction = parts.pop(0)
    else:
        direction = stats[1]
    dr, dc = DIRECTIONS[direction]
    def inside(r,c):
        return 0 <= r < len(grid) and 0 <= c < len(grid[0])
    if parts == ['A']:
        r,c = pos[0]+dr, pos[1]+dc
        return (inside(r,c) and grid[r][c] in ('G','S')
                and not (rules.team_mode and base and max(abs(r-base[0]),abs(c-base[1])) <= 1))
    if parts in (['F'], ['F','M'], ['F','S']):
        special = len(parts) == 2
        if stats[3 if special else 2] <= 0:
            return False
        for k in range(1, rules.shoot_range+1):
            r,c = pos[0]+dr*k,pos[1]+dc*k
            if not inside(r,c):
                break
            cell = grid[r][c]
            if cell in ('G','S','W'):
                continue
            return cell == 'T' or cell in s['enemies']
        return False
    if len(parts) == 2 and parts[0] == 'G':
        adjacent = any(inside(pos[0]+r,pos[1]+c) and grid[pos[0]+r][pos[1]+c] == 'F'
                       for r,c in DIRECTIONS.values())
        return (adjacent and stats[3] < rules.mega_capacity and s['codes']
                and parts[1] == 'BATTLESSAFY')
    return False


class BattleTests(unittest.TestCase):
    def action(self, s, **kwargs):
        rules = Rules(team_mode=False, **kwargs)
        command = BattleBot(rules).choose_action(s)
        self.assertTrue(legal(s, command, rules), command)
        return command

    def test_manual_cipher(self):
        self.assertEqual(decode_caesar('SRKKCVJJRWP', 17), 'BATTLESSAFY')
        self.assertEqual(decode_caesar('abc XYZ!', 0), 'abc XYZ!')

    def test_water_shot_at_range_three(self):
        s = state(['M W G X'], normal=1, target_hp=30)
        self.assertEqual(self.action(s), 'R F')

    def test_mega_finishes_before_incoming_fire(self):
        s = state(['E1 M X'], hp=10, normal=1, mega=1, target_hp=70)
        self.assertEqual(self.action(s), 'R F M')

    def test_do_not_waste_mega_for_thirty_hp(self):
        s = state(['M G X'], normal=1, mega=1, target_hp=30)
        self.assertEqual(self.action(s), 'R F')

    def test_ally_blocks_fire(self):
        s = state(['M M1 X','G G G'], mega=1)
        command = self.action(s)
        self.assertNotIn(command, ('R F','R F M'))

    def test_rock_blocks_fire(self):
        s = state(['M R X','G G G'], mega=1)
        self.assertNotIn(self.action(s), ('R F','R F M'))

    def test_tree_is_shot_and_turret_ammo_reserved(self):
        s = state(['M T G X'], normal=1, mega=1, target_hp=70)
        self.assertEqual(self.action(s), 'R F')

    def test_zero_ammo_never_fires(self):
        s = state(['M G G X','G G G G'], normal=0, mega=0)
        self.assertTrue(self.action(s).endswith(' A'))

    def test_supply_at_capacity_never_submits_g(self):
        s = state(['F M G X'], mega=10, codes=['SRKKCVJJRWP'])
        self.assertFalse(self.action(s).startswith('G '))

    def test_supply_manual_key(self):
        s = state(['F M G G G X'], normal=1, codes=['SRKKCVJJRWP'])
        self.assertEqual(self.action(s), 'G BATTLESSAFY')

    def test_supply_with_key_disabled(self):
        s = state(['F M G G X'], normal=1, codes=['SRKKCVJJRWP'])
        self.assertFalse(self.action(s,caesar_shift=None).startswith('G '))

    def test_cumulative_sand_damage(self):
        s = state(['M S S S G X','G G G G G G'], hp=20, normal=4)
        self.assertEqual(self.action(s), 'D A')

    def test_base_three_by_three_exclusion(self):
        s = state(['H G G G G','G G G G G','G M G G X','G G G G G'])
        rules=Rules()
        w=World(s,rules)
        commands=[x[0] for x in w.successors(w.start, w.enemy_index['X'], True)]
        self.assertNotIn('U A', commands)
        self.assertTrue(legal(s,BattleBot(rules).choose_action(s),rules))

    def test_two_tank_kills_immediate_win(self):
        # E3 is already destroyed, E2 is the winning second elimination.
        s=state(['E1 M E2 G X'], hp=10, normal=1, enemy_hp={'E2':30})
        bot=BattleBot()
        self.assertEqual(bot.choose_action(s),'R F')
        self.assertEqual(bot.reason,'Immediate team victory')

    def test_survive_to_finish_one_enemy(self):
        s=state(['M E1 G X'], hp=100, normal=1, mega=1)
        self.assertIn(self.action(s),('R F','R F M'))

    def test_no_wait_when_tree_is_only_action(self):
        s=state(['M T X'], normal=2)
        self.assertEqual(self.action(s),'R F')

    def test_dead_end_unavoidable_wait(self):
        s=state(['M R X'], normal=1)
        self.assertEqual(BattleBot(Rules(team_mode=False)).choose_action(s),'S')

    def test_parser_whitespace_and_crlf(self):
        raw='2  3  1 1 0\r\nM G X\r\nG G G\r\nM 100 R 99 0\r\nX 100\r\n'
        self.assertEqual(parse_data(raw)['allies']['M'][0],'100')

    def test_parser_truncation(self):
        with self.assertRaises(ValueError):
            parse_data('2 3 1 1 0\nM G X\nG G G')

    def test_input_not_mutated(self):
        s=state(['M G X'],normal=1,mega=1,target_hp=70)
        before=copy.deepcopy(s)
        self.action(s)
        self.assertEqual(s,before)

    def test_algorithm_exception_recovery_has_legal_choice(self):
        s=state(['M G X'],normal=1,mega=1)
        bot=BattleBot(Rules(team_mode=False))
        with patch.object(bot,'_search',side_effect=RuntimeError('injected')):
            try:
                command=bot.choose_action(s)
            except Exception:
                command=bot.emergency(World(s,bot.rules))
        self.assertTrue(legal(s,command,bot.rules))

    def test_socket_split_packet_and_sendall(self):
        payload=b'1 3 1 1 0\nM G X\nM 100 R 99 0\nX 100\n'
        class FakeSocket:
            def __init__(self):
                self.chunks=[payload[:4],payload[4:19],payload[19:-3],payload[-3:]]
                self.sent=[]
            def sendall(self,value): self.sent.append(value)
            def recv(self,limit): return self.chunks.pop(0) if self.chunks else b''
        bridge=LocalBridge()
        bridge.sock=FakeSocket()
        with patch('battle_bot.select.select',side_effect=lambda *args: ([bridge.sock] if bridge.sock.chunks else [],[],[])):
            result=bridge.submit('R F')
        self.assertEqual(parse_data(result)['enemies']['X'][0],'100')
        self.assertEqual(bridge.sock.sent,[b'R F '])

    def test_socket_end_marker(self):
        class FakeSocket:
            def sendall(self,value): pass
            def recv(self,limit): return b'0 Game Over\n'
        bridge=LocalBridge()
        bridge.sock=FakeSocket()
        self.assertIsNone(bridge.submit('R F'))

    def test_tiny_budget_still_chooses_route_around_wall(self):
        s=state(['R R R R R R R',
                 'R G R G G X R',
                 'R M R G G G R',
                 'R G R G G G R',
                 'R G G G G G R',
                 'R R R R R R R'])
        # A Manhattan-only fallback heads upward into a dead end here.
        self.assertEqual(self.action(s,max_nodes=1,time_budget_ms=10),'D A')

    def test_random_team_actions_are_legal(self):
        rng=random.Random(20261026)
        maximum=0
        for sample in range(120):
            size=8
            grid=[[rng.choices(['G','S','T','R','W'],[65,10,10,8,7])[0]
                   for _ in range(size)] for _ in range(size)]
            places=rng.sample([(r,c) for r in range(size) for c in range(size)],8)
            for name,(r,c) in zip(['M','H','X','E1','E2','E3','M1','M2'],places):
                grid[r][c]=name
            mr,mc=places[0]
            # At least one legal move or useful shot must exist for this test.
            candidates=[]
            for dr,dc in DIRECTIONS.values():
                r,c=mr+dr,mc+dc
                if 0 <= r<size and 0 <= c<size and grid[r][c] in ('G','S'):
                    br,bc=places[1]
                    if max(abs(r-br),abs(c-bc))>1:
                        candidates.append((r,c))
            if not candidates:
                continue
            s=state(grid,hp=rng.choice([10,30,70,100]),normal=rng.randrange(0,5),mega=rng.randrange(0,3))
            rules=Rules(time_budget_ms=20,max_nodes=300)
            start=time.perf_counter()
            command=BattleBot(rules).choose_action(s)
            maximum=max(maximum,time.perf_counter()-start)
            self.assertTrue(legal(s,command,rules),f'{sample}: {command}')
        self.assertLess(maximum,0.5)


if __name__=='__main__':
    unittest.main(verbosity=2)
