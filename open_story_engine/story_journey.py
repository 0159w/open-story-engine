"""Branch-local playthrough budgets and provisional, role-specific story plans.

Budgets guide generation. They never authorize actions, establish facts, or
close a route. Counts are rebuilt from saved ancestry, not model counters.
"""
import copy

from .reader_scene_plan import cjk_character_count

VERSION = 'story-journey/1'
BUDGET = dict(targetMinutes=150, typicalMinutes=[120, 180], targetCjk=30000,
              typicalCjk=[25000, 40000], targetTurns=45, typicalTurns=[40, 60])
PHASES = (
    ('establish', '建立目标与牵挂', 4500),
    ('develop', '行动、关系与代价', 10500),
    ('turn', '转折与关键抉择', 18000),
    ('resolve', '兑现伏笔与核心冲突', 25000),
    ('conclude', '结果与余波', 30000),
)


def enabled(node):
    value = node.get('storyJourney')
    return isinstance(value, dict) and value.get('version') == VERSION


def progress(nodes):
    # The caller supplies a single stored lineage. Defensively deduplicate IDs;
    # arc-selection menus and failed drafts are not readable story turns.
    seen, count, turns = set(), 0, 0
    for node in nodes:
        node_id = node.get('id')
        if node_id and node_id in seen:
            continue
        if node_id:
            seen.add(node_id)
        if node.get('kind') == 'arc_selection':
            continue
        text = node.get('narrativeText', '')
        count += cjk_character_count(text)
        if node.get('parentId') is not None and text.strip():
            turns += 1
    # Turns warn about too many tiny actions, but never prove plot progress.
    pressure = max(count / BUDGET['targetCjk'], turns / BUDGET['targetTurns'])
    index = next((i for i, phase in enumerate(PHASES)
                  if pressure < phase[2] / BUDGET['targetCjk']), len(PHASES) - 1)
    return dict(consumedCjk=count, completedTurns=turns,
                remainingCjk=max(0, BUDGET['targetCjk'] - count),
                remainingTurns=max(0, BUDGET['targetTurns'] - turns),
                suggestedPhase=PHASES[index][0],
                pacing='over_budget' if pressure >= 1 else 'closing' if index >= 3 else 'developing',
                plotCompletion=None)


def _text(value, maximum):
    return isinstance(value, str) and 0 < len(value.strip()) <= maximum


def validate_plan(value):
    """Only shape/size validation; all future content stays hypothetical."""
    if not isinstance(value, dict):
        return None
    if any(not _text(value.get(k), limit) for k, limit in
           (('premise', 240), ('centralQuestion', 180), ('revisionReason', 200))):
        return None
    phases = value.get('phases')
    if not isinstance(phases, list) or len(phases) != len(PHASES):
        return None
    cleaned = []
    for phase, (phase_id, _, end) in zip(phases, PHASES):
        if not isinstance(phase, dict) or phase.get('id') != phase_id:
            return None
        if any(not _text(phase.get(k), limit) for k, limit in
               (('title', 40), ('purpose', 180), ('payoff', 180))):
            return None
        cleaned.append(dict(id=phase_id, title=phase['title'].strip(),
                            purpose=phase['purpose'].strip(), payoff=phase['payoff'].strip(),
                            targetEndCjk=end))
    return dict(premise=value['premise'].strip(), centralQuestion=value['centralQuestion'].strip(),
                revisionReason=value['revisionReason'].strip(), phases=cleaned)


def outcome_observation(body, value):
    """Bind a pacing note to prose; absence never blocks saving or continuing."""
    if (not isinstance(value, dict) or value.get('basis') != 'observed'
            or not _text(value.get('summary'), 240)):
        return None
    quote, ref = value.get('evidenceQuote'), value.get('paragraphId')
    paragraphs = {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}
    if (not isinstance(ref, str) or ref not in paragraphs or not _text(quote, 600)
            or quote not in paragraphs[ref]):
        return None
    return dict(summary=value['summary'].strip(), paragraphId=ref,
                evidenceQuote=quote, basis='observed')


def phase_window(nodes, usage):
    # A planner's phase assessment is not a verified plot-completion state.
    phase_ids = [phase[0] for phase in PHASES]
    phase_id, count, turns, seen = None, 0, 0, set()
    for node in reversed(nodes):
        if node.get('kind') == 'arc_selection' or (node.get('id') and node['id'] in seen):
            continue
        if node.get('id'):
            seen.add(node['id'])
        saved = node.get('storyJourney') or {}
        review = (saved.get('review') or {}) if isinstance(saved, dict) else {}
        assessed = review.get('phaseId') if isinstance(review, dict) else None
        if assessed not in phase_ids or (phase_id is not None and phase_id != assessed):
            break
        phase_id = assessed
        count += cjk_character_count(node.get('narrativeText', ''))
        turns += int(node.get('parentId') is not None and bool(node.get('narrativeText', '').strip()))
    reasons = []
    if phase_id:
        index = phase_ids.index(phase_id)
        allowance = PHASES[index][2] - (PHASES[index - 1][2] if index else 0)
        if count >= allowance:
            reasons.append('phase_budget_spent')
        if index < phase_ids.index(usage['suggestedPhase']):
            reasons.append('budget_phase_ahead')
    return dict(assessedPhase=phase_id, consecutiveTurns=turns, consumedCjk=count,
                reviewReasons=reasons, authority='planning_assessment_not_completion')


def prepare(context):
    nodes = context.get('lineage') or [context['parent']]
    saved = context['parent'].get('storyJourney') or {}
    plan = validate_plan(saved.get('plan')) if isinstance(saved, dict) else None
    recent = []
    for node in nodes[-3:]:
        paragraphs = [p for p in node.get('narrativeText', '').split('\n\n') if p.strip()]
        tail = []
        for paragraph in reversed(paragraphs):
            if len(paragraph) + sum(map(len, tail)) > 600:
                break
            tail.insert(0, paragraph)
        recent.append(dict(branchId=node.get('id'),
            action=str((node.get('actionIntent') or {}).get('input', ''))[:300],
            summary=str(node.get('summary', ''))[:300], endingExcerpt='\n\n'.join(tail)))
        previous = node.get('storyJourney') or {}
        previous_review = (previous.get('review') or {}) if isinstance(previous, dict) else {}
        intended = previous_review.get('nextMilestone') if isinstance(previous_review, dict) else None
        recent[-1]['intendedMilestone'] = intended if _text(intended, 240) else None
        recent[-1]['observedOutcome'] = outcome_observation(node.get('narrativeText', ''),
            previous.get('lastOutcome') if isinstance(previous, dict) else None)
    revision = saved.get('revision', 0) if isinstance(saved, dict) else 0
    usage = progress(nodes)
    return dict(version=VERSION, budget=copy.deepcopy(BUDGET), progress=usage,
                phaseWindow=phase_window(nodes, usage),
                planBudgetPhase=saved.get('planBudgetPhase') if isinstance(saved, dict) else None,
                revision=revision if type(revision) is int and revision >= 0 else 0,
                review=dict(status='missing'), recentTurns=recent,
                recentTurnsBoundary='已保存原文与摘要；其中说法、猜测、安排不等于已核实或已完成。',
                role={k: context['contract']['persona'][k] for k in ('name', 'sourceCharacterId')
                      if k in context['contract']['persona']},
                plan=plan, planStatus='provisional' if plan else 'pending',
                phaseGuide=[dict(id=i, purpose=purpose, targetEndCjk=end) for i, purpose, end in PHASES],
                authority='future_possibilities_not_facts',
                rules=['按所选身份、既有关系、资源与世界条件形成完整故事，不受原著戏份和结局限制。',
                       '预算只指导推进密度，不是倒计时、最低字数或自动终局条件；暂停和阅读速度不改变故事。',
                       '不替玩家作重大决定；提前铺垫，在剩余篇幅内处理选择的后果，避免琐事和新增义务拖延。',
                       '未来阶段是假设，可随实际选择修改，不能当作已发生事实或泄露尚未揭示的答案。',
                       '原著终点不是本局终点；死亡、交付、毁灭不可撤销。提前终局可成立，不凑字续命。'])


def planning_view(journey):
    """Refresh stale future scaffolding at most once per budget phase.

    Keep the original journey for non-blocking fallback; only the planner's
    input omits the old future outline. Neither facts nor total usage change.
    """
    result = copy.deepcopy(journey)
    if (journey['phaseWindow']['reviewReasons']
            and journey.get('planBudgetPhase') != journey['progress']['suggestedPhase']):
        result.update(plan=None, planStatus='refresh_requested',
            refreshReason='同阶段已用篇幅较多且预算阶段变化；基于当前事实、玩家目标与剩余预算重排未来，不重新开始故事。')
    return result


def revise(journey, candidate, review=None):
    result = copy.deepcopy(journey)
    proposed = validate_plan(candidate)
    if (isinstance(review, dict) and review.get('decision') in ('keep', 'revise')
            and _text(review.get('reason'), 240) and _text(review.get('nextMilestone'), 240)):
        result['review'] = dict(status='recorded', decision=review['decision'],
            reason=review['reason'].strip(), nextMilestone=review['nextMilestone'].strip())
        if (review.get('phaseId') in [phase[0] for phase in PHASES]
                and _text(review.get('phaseReason'), 240)):
            result['review'].update(phaseId=review['phaseId'], phaseReason=review['phaseReason'].strip())
    if proposed is not None:
        old = journey.get('plan') or {}
        changed = any(old.get(key) != proposed[key] for key in ('premise', 'centralQuestion', 'phases'))
        result['revision'] = journey.get('revision', 0) + int(changed)
        result.update(plan=proposed, planStatus='provisional',
                      planBudgetPhase=journey['progress']['suggestedPhase'])
        result['review']['planChanged'] = changed
    elif candidate is not None or result['review'].get('decision') == 'revise':
        result['review']['status'] = 'revision_unavailable'
    return result


def ending_observation(body, data):
    """Record an ending described in this prose, never infer one from quota.

The existing observation model identifies the meaning; this only binds its
claim to a literal paragraph. No review, rewrite, or extra model call.
"""
    ending = data.get('journeyEnding') if isinstance(data, dict) else None
    if not isinstance(ending, dict) or ending.get('kind') not in ('completed', 'failed'):
        return None
    if ending.get('basis') != 'observed' or not _text(ending.get('summary'), 240):
        return None
    quote, ref = ending.get('evidenceQuote'), ending.get('paragraphId')
    paragraphs = {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}
    if (not isinstance(ref, str) or ref not in paragraphs or not _text(quote, 600)
            or quote not in paragraphs[ref]):
        return None
    return dict(kind=ending['kind'], summary=ending['summary'].strip(),
                paragraphId=ref, evidenceQuote=quote, basis='observed')


def record(context, journey, body, data):
    nodes = context.get('lineage') or [context['parent']]
    return dict(version=VERSION, budget=copy.deepcopy(BUDGET),
                progress=progress([*nodes, dict(parentId=context['parent']['id'], narrativeText=body)]),
                plan=copy.deepcopy(journey['plan']), planStatus=journey['planStatus'],
                revision=journey.get('revision', 0), review=copy.deepcopy(journey.get('review', {})),
                planBudgetPhase=journey.get('planBudgetPhase'),
                lastOutcome=outcome_observation(body, data.get('journeyProgress') if isinstance(data, dict) else None),
                authority='future_possibilities_not_facts', ending=ending_observation(body, data))


def public_view(nodes):
    if not enabled(nodes[-1]):
        return None
    # Do not leak planned titles, possible twists, or a false completion bar.
    return dict(target_minutes=BUDGET['targetMinutes'], typical_minutes=BUDGET['typicalMinutes'],
                target_cjk=BUDGET['targetCjk'], consumed_cjk=progress(nodes)['consumedCjk'],
                completed_turns=progress(nodes)['completedTurns'], estimate_only=True)
