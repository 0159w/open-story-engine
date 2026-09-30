"""Playthrough planning on the current official longform reader path."""
import copy
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from open_story_engine import story_journey as journey
from open_story_engine.api_journey import route_status
from open_story_engine.api_narrative import ContextNarrativePlanner
from open_story_engine.api_play import PlayService
from open_story_engine.api_models import RouteLifecycle, RouteClosurePreparation
from open_story_engine.api_read import ReadService
from open_story_engine.llm import Completion, LlmError
from open_story_engine.module_context import ModuleContextResolver
from tests_api.test_context_turn import ContextTurnTests
from tests_api.test_reader_consequences import ROOT, GU


def role_plan():
    return dict(premise='顾长离决定如何面对宗门的旧债。', centralQuestion='你要怎样承担自己的选择？',
                revisionReason='首次规划', phases=[dict(id=key, title=label,
                    purpose='围绕顾长离与当前人物的关系提供可改变局势的选择。',
                    payoff='若玩家追查，则让已公开的线索引出可执行的下一步。')
                    for key, label, _ in journey.PHASES])


class StoryJourneyTests(unittest.TestCase):
    setUp = ContextTurnTests.setUp
    gateway = ContextTurnTests.gateway
    generate = ContextTurnTests.generate
    commit = ContextTurnTests.commit

    def test_budget_refresh_omits_stale_future_only_and_uses_same_planning_call(self):
        old_plan = role_plan()
        old_plan['centralQuestion'] = '旧提纲独有的问题'
        self.context['parent']['storyJourney'] = dict(version=journey.VERSION, plan=old_plan,
            revision=1, planBudgetPhase='establish', review={'phaseId': 'establish'})
        revised = role_plan()
        revised['centralQuestion'] = '当前谋生的选择与代价'
        self.plan['journeyPlan'] = revised
        with patch.object(journey, 'phase_window', return_value={'reviewReasons': ['phase_budget_spent']}), \
             patch.object(journey, 'progress', return_value={'suggestedPhase': 'develop'}):
            gateway = self.gateway()
            result, _ = self.generate(gateway)
        planner_input = gateway.complete_json.call_args_list[0].args[0][-1]['content']
        self.assertNotIn('旧提纲独有的问题', planner_input)
        self.assertIn('refresh_requested', planner_input)
        self.assertEqual(result['storyJourney']['planBudgetPhase'], 'develop')
        self.assertEqual(result['storyJourney']['revision'], 2)
        self.assertEqual(gateway.complete_json.call_count, 2)
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertIn('当前谋生的选择与代价', gateway.complete_text.call_args.args[0][-1]['content'])
        self.commit(result)

    def test_refresh_failure_keeps_original_plan_and_counts_without_retry(self):
        self.context['parent']['storyJourney'] = dict(version=journey.VERSION, plan=role_plan(),
            revision=2, planBudgetPhase='establish')
        self.plan['journeyPlan'] = {'premise': 'broken'}
        with patch.object(journey, 'phase_window', return_value={'reviewReasons': ['phase_budget_spent']}), \
             patch.object(journey, 'progress', return_value={'suggestedPhase': 'develop'}):
            gateway = self.gateway()
            result, _ = self.generate(gateway)
        self.assertEqual(result['storyJourney']['plan'], journey.validate_plan(role_plan()))
        self.assertEqual(result['storyJourney']['planBudgetPhase'], 'establish')
        self.assertEqual(result['narrativeText'], self.body)
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.commit(result)
        prepared = journey.prepare(self.context)
        prepared['phaseWindow']['reviewReasons'] = ['phase_budget_spent']
        prepared['planBudgetPhase'] = prepared['progress']['suggestedPhase']
        self.assertEqual(journey.planning_view(prepared)['plan'], prepared['plan'])

    def test_actual_outcome_and_intended_milestone_stay_distinct_across_turns(self):
        self.body = '执事收下你交回的木牌，注销了试炼资格。'
        self.plan['journeyReview'] = dict(decision='keep', reason='先办退试。',
            phaseId='establish', phaseReason='尚未离山。', nextMilestone='交牌退试并抵达山下。')
        self.data['journeyProgress'] = dict(summary='木牌已交回，尚未抵达山下。', basis='observed',
            paragraphId='P1', evidenceQuote=self.body)
        gateway = self.gateway()
        result, _ = self.generate(gateway)
        self.commit(result)
        parent = {**self.context['parent'], 'storyJourney': result['storyJourney'],
                  'narrativeText': self.body}
        prepared = journey.prepare({**self.context, 'parent': parent, 'lineage': [parent]})
        recent = prepared['recentTurns'][0]
        self.assertEqual(recent['intendedMilestone'], '交牌退试并抵达山下。')
        self.assertEqual(recent['observedOutcome']['summary'], '木牌已交回，尚未抵达山下。')
        self.assertEqual(prepared['phaseWindow']['assessedPhase'], 'establish')
        self.assertNotIn('journeyProgress', result['consequenceUpdate'])
        self.assertEqual(gateway.complete_json.call_count, 2)
        self.assertEqual(gateway.complete_text.call_count, 1)
        self.assertEqual(result['narrativeText'], self.body)
        self.context = {**self.context, 'parent': parent, 'lineage': [parent]}
        next_gateway = self.gateway()
        self.generate(next_gateway)
        writer = next_gateway.complete_text.call_args.args[0][-1]['content']
        self.assertIn('木牌已交回，尚未抵达山下。', writer)
        self.assertIn('intendedMilestone', writer)

    def test_invalid_outcome_is_unknown_and_cannot_discard_prose(self):
        for change in ({'basis': 'reported'}, {'paragraphId': 'P99'},
                       {'evidenceQuote': '没有发生的结果'}, {'summary': []}):
            with self.subTest(change=change):
                self.data['journeyProgress'] = dict(summary='结果', basis='observed',
                    paragraphId='P1', evidenceQuote=self.body.split('\n\n')[0])
                self.data['journeyProgress'].update(change)
                result, _ = self.generate(self.gateway())
                self.assertIsNone(result['storyJourney']['lastOutcome'])
                self.assertEqual(result['narrativeText'], self.body)
                self.assertFalse(result['naturalEnding'])
                self.commit(result)

    def test_phase_window_uses_saved_lineage_and_survives_replanning_without_resetting_budget(self):
        nodes = [dict(id=f'b{i}', parentId='root', narrativeText=self.body,
            storyJourney={'review': {'phaseId': 'develop'}, 'revision': i + 1}) for i in range(4)]
        usage = journey.progress(nodes)
        self.assertEqual(journey.phase_window([*nodes, nodes[-1]], usage)['consecutiveTurns'], 4)
        self.assertEqual(journey.phase_window(nodes, usage)['consumedCjk'], usage['consumedCjk'])
        pressured = {**usage, 'suggestedPhase': 'turn'}
        window = journey.phase_window(nodes, pressured)
        self.assertEqual(window['reviewReasons'], ['budget_phase_ahead'])
        self.assertEqual(window['authority'], 'planning_assessment_not_completion')
        with patch.object(journey, 'cjk_character_count', return_value=1500):
            self.assertEqual(journey.phase_window(nodes, pressured)['reviewReasons'],
                             ['phase_budget_spent', 'budget_phase_ahead'])
        nodes[-1]['storyJourney']['review']['phaseId'] = 'turn'
        self.assertEqual(journey.phase_window(nodes, pressured)['consecutiveTurns'], 1)
        self.assertEqual(journey.progress(nodes), usage)

    def test_old_save_missing_or_malformed_pacing_notes_remains_usable(self):
        for saved in ({}, {'review': 'bad', 'lastOutcome': []}):
            with self.subTest(saved=saved):
                parent = {**self.context['parent'], 'storyJourney': saved}
                prepared = journey.prepare({**self.context, 'parent': parent, 'lineage': [parent]})
                self.assertIsNone(prepared['phaseWindow']['assessedPhase'])
                self.assertIsNone(prepared['recentTurns'][0]['observedOutcome'])
                revised = journey.revise(prepared, None, dict(decision='keep', reason='继续',
                    nextMilestone='新结果', phaseId='invalid', phaseReason='无效阶段'))
                self.assertEqual(revised['review']['status'], 'recorded')
                self.assertNotIn('phaseId', revised['review'])

    def test_preflight_failure_retains_diagnostics_without_calling_model(self):
        gateway = self.gateway()
        planner = ContextNarrativePlanner(gateway)
        planner.last_prompt_context = {'contextBundle': {'status': 'rejected', 'reason': 'unresolved_path'}}
        with patch.object(planner, '_preflight_context_projection',
                          side_effect=LlmError('缺少上下文字段', 'context_projection_rejected')):
            with self.assertRaises(LlmError) as caught:
                planner.plan(self.context, dict(id='custom', title=self.action, summary=self.action,
                    isFreeText=True, statePatch={}), self.context['parent']['branchState'])
        self.assertEqual(caught.exception.audit['failureStage'], 'context_preflight')
        self.assertEqual(caught.exception.audit['promptContext']['contextBundle']['reason'], 'unresolved_path')
        gateway.complete_json.assert_not_called()
        gateway.complete_text.assert_not_called()

    def test_reroute_revision_reaches_writer_and_is_not_a_fact(self):
        self.context['parent']['storyJourney'] = dict(version=journey.VERSION, plan=role_plan(), revision=1)
        changed = role_plan()
        changed.update(centralQuestion='离开宗门后，你如何与同行者重新生活？', revisionReason='玩家明确放下旧案，决定离开。')
        self.plan['journeyPlan'] = changed
        self.plan['journeyReview'] = dict(decision='revise', reason='放下旧案，改为离开。', nextMilestone='离开后的第一个实际落脚点。')
        gateway = self.gateway()
        result, _ = self.generate(gateway)
        self.assertEqual(result['storyJourney']['revision'], 2)
        self.assertTrue(result['storyJourney']['review']['planChanged'])
        self.assertIn(changed['centralQuestion'], gateway.complete_text.call_args.args[0][-1]['content'])
        self.assertNotIn(changed['centralQuestion'], json.dumps(result['consequenceUpdate'], ensure_ascii=False))
        self.assertEqual(gateway.complete_json.call_count, 2)
        self.commit(result)

    def test_unavailable_revision_does_not_drop_prose_or_old_plan(self):
        self.context['parent']['storyJourney'] = dict(version=journey.VERSION, plan=role_plan(), revision=2)
        self.plan['journeyReview'] = dict(decision='revise', reason='玩家改道。', nextMilestone='抵达新地点。')
        result, _ = self.generate(self.gateway())
        self.assertEqual(result['storyJourney']['review']['status'], 'revision_unavailable')
        self.assertEqual(result['storyJourney']['revision'], 2)
        self.assertEqual(result['narrativeText'], self.body)
        self.commit(result)

    def test_recent_endings_are_bounded_verbatim_and_ancestry_only(self):
        original = copy.deepcopy(self.context['parent'])
        context = {**self.context, 'lineage': [dict(id=f'b{i}', parentId='root',
            narrativeText=self.body, actionIntent={'input': self.action}, summary='已有摘要') for i in range(5)]}
        prepared = journey.prepare(context)
        self.assertEqual([n['branchId'] for n in prepared['recentTurns']], ['b2', 'b3', 'b4'])
        for node in prepared['recentTurns']:
            self.assertLessEqual(len(node['endingExcerpt']), 610)
            for paragraph in node['endingExcerpt'].split('\n\n'):
                self.assertIn(paragraph, self.body)
        self.assertEqual(self.context['parent'], original)

    def test_plan_reaches_writer_and_saved_branch_without_extra_call(self):
        self.plan['journeyPlan'] = role_plan()
        gateway = self.gateway()
        result, audit = self.generate(gateway)
        self.commit(result)
        saved = result['storyJourney']
        self.assertEqual(saved['plan']['centralQuestion'], role_plan()['centralQuestion'])
        self.assertEqual(saved['authority'], 'future_possibilities_not_facts')
        self.assertEqual(saved['progress']['completedTurns'], 1)
        self.assertFalse(result['naturalEnding'])
        self.assertEqual(gateway.complete_json.call_count, 2)
        self.assertEqual(gateway.complete_text.call_count, 1)
        writer = gateway.complete_text.call_args.args[0][-1]['content']
        self.assertIn(role_plan()['premise'], writer)
        self.assertFalse(audit['promptContext']['storyJourney']['automaticLengthGate'])
        self.assertNotIn('storyJourney', result['consequenceUpdate'])

    def test_required_bundle_carries_budget_as_output_goal_not_fact(self):
        self.plan['journeyPlan'] = role_plan()
        gateway = self.gateway()
        resolver = ModuleContextResolver.for_package(
            ROOT / 'content/packages/taixu-relics-part1/0.1.3/package.json', self.fixture.package)
        self.context.update(validatedStatePatch={}, validatedStatePatchSource='apply_branch_patch')
        planner = ContextNarrativePlanner(gateway, context_resolver=resolver, require_context_bundle=True)
        result, _ = planner.plan(self.context, dict(id='custom', title=self.action, summary=self.action,
            isFreeText=True, statePatch={}), self.fixture.root['branchState'])
        projection = planner.last_context_bundle.project('result_contract')
        self.assertEqual(projection['outputContract']['storyJourney']['budget']['targetCjk'], 30000)
        self.assertNotIn('storyJourney', projection['authoritativeState'])
        self.assertEqual(result['storyJourney']['plan']['premise'], role_plan()['premise'])

    def test_malformed_revision_keeps_previous_plan_and_prose(self):
        self.context['parent']['storyJourney'] = dict(version=journey.VERSION, plan=role_plan())
        original = copy.deepcopy(self.context['parent'])
        self.plan['journeyPlan'] = dict(premise='broken')
        result, _ = self.generate(self.gateway())
        self.assertEqual(result['storyJourney']['plan']['premise'], role_plan()['premise'])
        self.assertEqual(self.context['parent'], original)
        self.assertEqual(result['narrativeText'], self.body)
        self.commit(result)

    def test_over_budget_does_not_close_or_discard_failed_extraction(self):
        with patch.object(journey, 'progress', return_value=dict(consumedCjk=61000, completedTurns=91,
                remainingCjk=0, remainingTurns=0, suggestedPhase='conclude', pacing='over_budget', plotCompletion=None)):
            result, _ = self.generate(self.gateway(LlmError('timeout', 'timeout')))
        self.assertFalse(result['naturalEnding'])
        self.assertEqual(result['narrativeText'], self.body)
        self.assertEqual(result['storyJourney']['progress']['remainingCjk'], 0)
        self.assertIsNone(result['storyJourney']['lastOutcome'])
        self.commit(result)

    def test_ending_uses_observed_prose_even_before_budget_and_binds_receipt(self):
        self.body = '你与同伴安葬了逝者，旧案终于有了答案。这段追索至此结束，往后的日子由你自己选择。'
        self.data['journeyEnding'] = dict(kind='completed', basis='observed', summary='旧案已了，告别追索。',
            paragraphId='P1', evidenceQuote='这段追索至此结束，往后的日子由你自己选择。')
        result, _ = self.generate(self.gateway())
        self.assertTrue(result['naturalEnding'])
        self.assertEqual(result['nextDirections'], [])
        self.assertLess(result['storyJourney']['progress']['consumedCjk'], 25000)
        self.commit(result)
        with self.assertRaisesRegex(ValueError, '终局观察'):
            self.commit({**result, 'naturalEnding': False})

    def test_failed_ending_does_not_mark_story_goal_successful(self):
        self.body = '你在坍塌中死亡。这段旅程随之终止，未能完成的愿望留给了同行者。'
        self.data['journeyEnding'] = dict(kind='failed', basis='observed', summary='玩家死亡，愿望未成。',
            paragraphId='P1', evidenceQuote='你在坍塌中死亡。')
        result, _ = self.generate(self.gateway())
        self.assertTrue(result['naturalEnding'])
        self.assertEqual(result['storyArc']['goalDisposition'], 'failed')
        self.commit(result)

    def test_unbound_ending_or_plan_cannot_end_story(self):
        for ending in (dict(kind='completed', basis='observed', summary='到达字数',
                            paragraphId='P9', evidenceQuote=self.body),
                       dict(kind='failed', basis='reported', summary='有人说你死了',
                            paragraphId='P1', evidenceQuote=self.body.split('\n\n')[0])):
            self.data['journeyEnding'] = ending
            result, _ = self.generate(self.gateway())
            self.assertFalse(result['naturalEnding'])
            self.assertEqual(result['narrativeText'], self.body)
            self.commit(result)

    def test_saved_ancestry_counts_only_active_route_and_never_trusts_counter(self):
        root = copy.deepcopy(self.fixture.root)
        root['parentId'] = None
        child = dict(id='child', parentId=root['id'], narrativeText=self.body,
                     storyJourney=dict(version=journey.VERSION, progress=dict(consumedCjk=999999)))
        counts = journey.progress([root, child, child])
        self.assertEqual(counts['completedTurns'], 1)
        self.assertEqual(counts['consumedCjk'], journey.cjk_character_count(root['narrativeText'] + self.body))
        menu = dict(id='menu', parentId='child', kind='arc_selection', narrativeText='只选择方向')
        self.assertEqual(journey.progress([root, child, menu]), counts)
        other = dict(id='other', parentId=root['id'], narrativeText='另一条路。')
        self.assertNotEqual(journey.progress([root, other])['consumedCjk'], counts['consumedCjk'])
        self.assertEqual(journey.progress([root])['completedTurns'], 0)

    def test_source_endpoint_and_empty_menu_do_not_close_new_journey(self):
        node = copy.deepcopy(self.fixture.root)
        node.update(storyJourney=dict(version=journey.VERSION), nextDirections=[], sourceNodeRef='last')
        package = dict(story=dict(narrativeGraph=dict(endingBeatIds={'normal': 'last'})))
        self.assertFalse(PlayService._natural_ending(node, package))
        with patch('open_story_engine.api_journey.preferences', return_value={'ended': {}}):
            self.assertEqual(route_status(Mock(), 'sid', [node], package, terminal=True), 'active')
            node['naturalEnding'] = True
            self.assertEqual(route_status(Mock(), 'sid', [node], package, terminal=False), 'completed')
        self.assertTrue(PlayService._natural_ending(node, package))

    def test_public_view_has_no_future_plan_or_completion_percentage(self):
        node = dict(self.fixture.root, storyJourney=dict(version=journey.VERSION, plan=role_plan()))
        public = journey.public_view([node])
        self.assertEqual(public['target_minutes'], 150)
        self.assertTrue(public['estimate_only'])
        self.assertNotIn(role_plan()['centralQuestion'], json.dumps(public, ensure_ascii=False))
        self.assertNotIn('progress', public)

    def test_observed_ending_receipt_matches_public_lifecycle_schema(self):
        from open_story_engine.storage import SessionStore
        from open_story_engine import route_lifecycle
        from open_story_engine.api_journey import save_preferences
        store = SessionStore(':memory:')
        try:
            session = store.create_session(self.fixture.package)
            sid = session['id']
            root = store.create_branch_root(sid, self.fixture.root)
            root['storyJourney'] = dict(version=journey.VERSION, ending=dict(kind='failed',
                basis='observed', summary='玩家死亡', paragraphId='P1', evidenceQuote='玩家死亡'))
            # Exercise the real lifecycle persistence and response schema with
            # a bound stored node; the semantic ending is tested separately.
            store.connection.execute('UPDATE branch_nodes SET node_json=? WHERE id=?',
                                     (json.dumps(root, ensure_ascii=False), root['id']))
            save_preferences(store, sid, title='终局回执测试')
            PlayService._save_natural_ending(store, sid, root['id'])
            value = route_lifecycle.view(store, sid, [root], dict(status='completed'))
            self.assertEqual(RouteLifecycle.model_validate(value).ending_type, 'failure')
            self.assertEqual(value['receipt']['source'], 'observed_narrative')
            self.assertNotIn('proposal_id', value['receipt'])
        finally:
            store.close()

    def test_api_old_save_adoption_replay_and_fork_keep_independent_counts(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
            read = ReadService(ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
            play = PlayService(read, Path(tmp))
            self.addCleanup(play.drafts.close)
            start = play.create_session('taixu-relics-part1', '0.1.3', 'entry_gu_trial', GU, identity_opening=True)
            self.assertNotIn('storyJourney', start['branch'])
            self.plan['journeyPlan'] = role_plan()
            gateway = self.gateway()
            def response(messages, **kwargs):
                if 'journeyPlan' in messages[0]['content']:
                    return Completion(json.dumps(self.plan), '{}', [])
                if 'journeyEnding' in messages[0]['content']:
                    return Completion(json.dumps(self.data), '{}', [])
                return Completion('{"choices":[]}', '{}', [])
            gateway.complete_json.side_effect = response
            text_call = gateway.complete_text
            play._planner = ContextNarrativePlanner(gateway)
            sid, bid = start['session']['id'], start['branch']['id']
            first = play.continue_turn(sid, bid, text=self.action, request_id='one')
            calls = text_call.call_count
            replay = play.continue_turn(sid, bid, text=self.action, request_id='one')
            fork_gateway = self.gateway()
            fork_gateway.complete_json.side_effect = response
            play._planner = ContextNarrativePlanner(fork_gateway)
            fork = play.continue_turn(sid, bid, text='我在原地静候答复。', request_id='fork')
            self.assertEqual(replay['branch']['id'], first['branch']['id'])
            self.assertEqual(text_call.call_count, calls)
            self.assertEqual(first['branch']['storyJourney']['progress'], fork['branch']['storyJourney']['progress'])
            view = read.journey(sid, fork['branch']['id'])
            self.assertEqual(view['playthrough_length']['completed_turns'], 1)
            self.assertIsNone(view['progress'])
            with read.store() as store:
                self.assertNotIn('storyJourney', store.branch(sid, bid))
            play.end_route(sid, fork['branch']['id'])
            abandoned = read.journey(sid, fork['branch']['id'])
            self.assertEqual(abandoned['status'], 'abandoned')
            self.assertIsNone(abandoned['progress'])
            self.assertNotIn('走到这条路线的结局', [m['label'] for m in abandoned['milestones']])

    def test_api_ending_commits_without_choice_call_and_closure_response_validates(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
            read = ReadService(ROOT / 'content/packages', Path(tmp) / 'sessions.sqlite')
            play = PlayService(read, Path(tmp))
            self.addCleanup(play.drafts.close)
            start = play.create_session('taixu-relics-part1', '0.1.3', 'entry_gu_trial', GU, identity_opening=True)
            self.body = '你查清了旧案，与故人告别，这段追索至此结束。'
            self.data['journeyEnding'] = dict(kind='completed', basis='observed', summary='旧案终结。',
                paragraphId='P1', evidenceQuote=self.body)
            gateway = self.gateway()
            json_call = gateway.complete_json
            play._planner = ContextNarrativePlanner(gateway)
            sid = start['session']['id']
            result = play.continue_turn(sid, start['branch']['id'], text=self.action, request_id='end')
            bid = result['branch']['id']
            self.assertEqual(json_call.call_count, 2)
            self.assertEqual(result['branch']['nextDirections'], [])
            self.assertEqual(read.journey(sid, bid)['status'], 'completed')
            closure = RouteClosurePreparation.model_validate(read.route_closure(sid, bid))
            self.assertTrue(closure.ending_written)
            self.assertEqual(closure.lifecycle.receipt.source, 'observed_narrative')


def load_tests(loader, _tests, _pattern):
    return loader.loadTestsFromTestCase(StoryJourneyTests)
