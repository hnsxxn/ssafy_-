import copy
import importlib.util
from pathlib import Path
import random
import unittest
from unittest.mock import patch
from battle_bot import BattleBot, Rules, World, check_action
from test_battle_bot import state, legal, DIRECTIONS


def open_team(size=16, pos=(13,2), mega=1):
    grid=[['G']*size for _ in range(size)]
    units={'M':pos,'H':(size-1,0),'X':(0,size-1),'E1':(1,size-2),
           'E2':(1,size-3),'E3':(2,size-2),'M1':(size-4,1),'M2':(size-2,3)}
    for name,(r,c) in units.items():
        grid[r][c]=name
    return state(grid,normal=99,mega=mega)


def apply_move(s, command):
    if command.endswith(' A'):
        dr,dc=DIRECTIONS[command[0]]
        pos=next((r,c) for r,row in enumerate(s['grid']) for c,x in enumerate(row) if x=='M')
        s['grid'][pos[0]][pos[1]]='G'
        dest=pos[0]+dr,pos[1]+dc
        if s['grid'][dest[0]][dest[1]]=='S':
            s['allies']['M'][0]-=10
        s['grid'][dest[0]][dest[1]]='M'
        s['allies']['M'][1]=command[0]


class RolesRegressionTests(unittest.TestCase):
    def test_empty_shot_rejected(self):
        s=state(['M G G G X'])
        self.assertFalse(check_action(s,'R F',Rules(team_mode=False)))

    def test_dead_enemy_is_not_a_valid_target(self):
        s=state(['M E1 X'],enemy_hp={'E1':0})
        self.assertFalse(check_action(s,'R F',Rules(team_mode=False)))

    def test_facility_blocks_a_target(self):
        s=state(['M F X'])
        self.assertFalse(check_action(s,'R F M',Rules(team_mode=False)))

    def test_ally_blocks_a_target_final_gate(self):
        s=state(['M M1 X'],mega=1)
        self.assertFalse(check_action(s,'R F M',Rules(team_mode=False)))

    def test_raw_snapshot_gate_replaces_projected_ghost_shot(self):
        s=state(['M G G G X','G G G G G'])
        bot=BattleBot(Rules(team_mode=False))
        with patch.object(bot,'_attack',return_value='R F'):
            command=bot.choose_action(s)
        self.assertNotEqual(command,'R F')
        self.assertTrue(legal(s,command,bot.rules))

    def test_same_missed_shot_not_repeated(self):
        s=state(['M G X','G G G'],target_hp=100,mega=1)
        bot=BattleBot(Rules(team_mode=False))
        first=bot.choose_action(s)
        self.assertIn(first,('R F','R F M'))
        second=bot.choose_action(s)
        self.assertNotIn(second,('R F','R F M'))
        self.assertTrue(legal(s,second,bot.rules))
        self.assertIn('no observed HP drop',bot.feedback)

    def test_successful_shot_can_be_followed_up(self):
        s=state(['M G X','G G G'],target_hp=100,mega=1)
        bot=BattleBot(Rules(team_mode=False))
        first=bot.choose_action(s)
        special=first.endswith('F M')
        s['enemies']['X'][0]-=70 if special else 30
        s['allies']['M'][3 if special else 2]-=1
        second=bot.choose_action(s)
        self.assertIn(second,('R F','R F M'))
        self.assertFalse(bot.feedback)

    def test_long_dry_detour_precedes_sand_shortcut(self):
        s=state(['R R R R R R R R',
                 'M S S G G G X R',
                 'G R R R R R G R',
                 'G R G G G G G R',
                 'G R G R R R R R',
                 'G R G R G G G R',
                 'G R G R G R G R',
                 'G R G G G R G R',
                 'G R R R R R G R',
                 'G G G G G G G R'])
        bot=BattleBot(Rules(team_mode=False,max_nodes=1))
        self.assertEqual(bot.choose_action(s),'D A')

    def test_sand_allowed_only_if_dry_objective_route_absent(self):
        s=state(['M S S G G X'],normal=4)
        bot=BattleBot(Rules(team_mode=False))
        self.assertEqual(bot.choose_action(s),'R A')
        self.assertTrue(bot._sand_required)

    def test_lethal_sand_step_not_chosen_with_a_dry_escape(self):
        s=state(['M S G G X','G R R R R'],hp=10,normal=4)
        bot=BattleBot(Rules(team_mode=False))
        self.assertEqual(bot.choose_action(s),'D A')

    def test_attacker_prioritizes_turret_over_nonwinning_tank_kill(self):
        s=open_team(pos=(1,11))
        # Place enemies away from the attacker; one low-HP enemy is also in range.
        for r,row in enumerate(s['grid']):
            for c,cell in enumerate(row):
                if cell.startswith('E'): row[c]='G'
        for name,pos in {'E1':(3,13),'E2':(9,9),'E3':(10,10)}.items():
            s['grid'][pos[0]][pos[1]]=name
        s['grid'][1][11]='G';s['grid'][1][13]='M'
        s['enemies']['E1'][0]=30
        # The turret at (0,15) isn't aligned; move it into a safe firing lane.
        s['grid'][0][15]='G';s['grid'][1][15]='X'
        # Enemy behind a rock cannot attack us, yet a safe turret shot is available.
        s['grid'][2][13]='R'
        bot=BattleBot(Rules(player_slot=1))
        self.assertIn(bot.choose_action(s),('R F','R F M'))
        self.assertEqual(bot.reason,'Attacker: safe turret damage')

    def test_defender_targets_enemy_attacking_our_base(self):
        s=open_team(pos=(13,2))
        s['grid'][1][14]='G';s['grid'][15][3]='E1'
        # Put player in the same row, outside the forbidden area.
        s['grid'][13][2]='G';s['grid'][15][5]='M'
        bot=BattleBot(Rules(player_slot=2))
        self.assertIn(bot.choose_action(s),('L F','L F M'))
        self.assertEqual(bot.reason,'Defender: intercept an approaching enemy')

    def test_defenders_do_not_chase_distant_enemies(self):
        for slot in (2,3):
            s=open_team()
            bot=BattleBot(Rules(player_slot=slot,time_budget_ms=25,max_nodes=300))
            for turn in range(30):
                command=bot.choose_action(s)
                self.assertTrue(legal(s,command,bot.rules),(slot,turn,command))
                self.assertNotIn(' F',command)
                apply_move(s,command)
                pos=next((r,c) for r,row in enumerate(s['grid']) for c,x in enumerate(row) if x=='M')
                self.assertLessEqual(max(abs(pos[0]-15),abs(pos[1])),bot.rules.defense_leash)
                self.assertGreater(max(abs(pos[0]-15),abs(pos[1])),1)

    def test_defenders_choose_separate_guard_flanks(self):
        s=open_team()
        first=BattleBot(Rules(player_slot=2));second=BattleBot(Rules(player_slot=3))
        a=first._guard_cells(World(s,first.rules));b=second._guard_cells(World(s,second.rules))
        self.assertTrue(a and b)
        self.assertFalse(a & b)

    def test_defender_patrol_avoids_sand_when_dry_moves_exist(self):
        s=open_team()
        s['grid'][13][3]='S';s['grid'][14][2]='S'
        bot=BattleBot(Rules(player_slot=3,time_budget_ms=30,max_nodes=400))
        command=bot.choose_action(s)
        self.assertTrue(legal(s,command,bot.rules))
        if command.endswith(' A'):
            dr,dc=DIRECTIONS[command[0]]
            self.assertNotEqual(s['grid'][13+dr][2+dc],'S')

    def test_standing_on_known_sand_preserves_underlying_terrain(self):
        s=state(['M G G G X','G G G G G'])
        w=World(s,Rules(team_mode=False),{(0,0):'S'})
        self.assertEqual(w.cell((0,0),w.start),'S')

    def test_final_gate_checks_ammunition_again(self):
        s=state(['M G X'],normal=0,mega=0)
        self.assertFalse(check_action(s,'R F',Rules()))
        self.assertFalse(check_action(s,'R F M',Rules()))

    def test_final_gate_cannot_shoot_through_rock(self):
        s=state(['M R X'])
        self.assertFalse(check_action(s,'R F',Rules()))

    def test_own_tank_shields_the_base(self):
        s=state(['H G M E1 G X'])
        w=World(s,Rules(player_slot=2))
        self.assertEqual(w.incoming_base(w.start),0)
        self.assertGreater(w.incoming(w.start),0)

    def test_defender_blocks_lethal_base_shot(self):
        s=open_team(pos=(14,2))
        s['grid'][1][14]='G';s['grid'][15][3]='E1'
        s['allies']['H'][0]=30
        bot=BattleBot(Rules(player_slot=2))
        self.assertEqual(bot.choose_action(s),'D A')
        self.assertEqual(bot.reason,'Defender: prevent a lethal base volley')

    def test_defender_may_use_sand_to_save_the_base(self):
        s=open_team(pos=(14,2))
        s['grid'][1][14]='G';s['grid'][15][3]='E1';s['grid'][15][2]='S'
        s['allies']['H'][0]=30
        bot=BattleBot(Rules(player_slot=3))
        self.assertEqual(bot.choose_action(s),'D A')
        self.assertTrue(bot._sand_required)

    def test_guard_leash_applies_even_in_fallback(self):
        s=open_team(pos=(9,6))
        bot=BattleBot(Rules(player_slot=2))
        world=World(s,bot.rules)
        self.assertFalse(world.movable((8,6),world.start))
        self.assertFalse(world.movable((9,7),world.start))

    def test_random_roles_actions_obey_latest_snapshot(self):
        rng=random.Random(20261027)
        for slot in (1,2,3):
            for sample in range(60):
                s=open_team(size=16)
                for r in range(16):
                    for c in range(16):
                        if s['grid'][r][c]=='G':
                            s['grid'][r][c]=rng.choices(['G','S','T','R','W'],[70,12,8,7,3])[0]
                s['grid'][12][2]='G'
                rules=Rules(player_slot=slot,time_budget_ms=15,max_nodes=150)
                command=BattleBot(rules).choose_action(s)
                self.assertTrue(legal(s,command,rules),(slot,sample,command))
                self.assertTrue(check_action(s,command,rules))


if __name__=='__main__':
    unittest.main(verbosity=2)
