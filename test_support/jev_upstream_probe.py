"""独立上游提取实验；不接入正文流程，不发送候选正文，不修改存档。"""
import argparse
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from open_story_engine.environment import load_env_file
from open_story_engine.jev_gateway import JevGatewayConfig


QUESTIONS = {
    'intent': {'type': 'choice', 'instructions': '只按playerAction选本回合主要意图；说明将来打算不等于现在行动。',
               'criteria': {'inform': '告知或说明打算', 'ask': '询问并等待回答',
                            'wait': '保持位置或准备后等待', 'act': '立即执行其他行动', 'unknown': '无法确定'}},
    'movement': {'type': 'choice', 'instructions': 'playerAction是否要求现在离开当前地点？未来计划不算。',
                 'criteria': {'leave': '明确现在离开', 'stay': '明确现在留下或等待', 'unknown': '未明确'}},
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    load_env_file(root / '.env')
    config = JevGatewayConfig.from_env()
    if not config.api_key:
        raise SystemExit('缺少Jev凭据')
    fixture = json.loads((root / 'test_support/fixtures/jev-real-longform-review.json').read_text())
    results = []
    for case in fixture['cases'][:3]:
        # Candidate prose is deliberately excluded: these are pre-writing inputs.
        state = {k: case[k] for k in ('context', 'branchState', 'playerAction')}
        body = dict(model=config.model, state=state, questions=QUESTIONS)
        request = Request(config.base_url.rstrip('/') + '/api/alpha/decisions',
                          data=json.dumps(body, ensure_ascii=False).encode(),
                          headers={'Authorization': 'Bearer ' + config.api_key, 'Content-Type': 'application/json'}, method='POST')
        result = dict(id=case['id'], input=state, baselineInputChars=len(json.dumps(state, ensure_ascii=False)))
        started = time.monotonic()
        try:
            with urlopen(request, timeout=config.timeout_seconds) as response:
                payload = json.loads(response.read())
            answers = payload.get('answers', {})
            valid = isinstance(answers, dict) and all(isinstance(answers.get(k), dict) and answers[k].get('choice') in q['criteria']
                        for k, q in QUESTIONS.items())
            result.update(status='ok' if valid else 'invalid', answers=answers, usage=payload.get('usage'))
            if valid:
                structured = {k: answers[k]['choice'] for k in QUESTIONS}
                # Tags alone cannot preserve facts, so all authoritative inputs
                # remain verbatim. Never claim their removal as a speed gain.
                assembled = {**state, 'actionTags': structured}
                result['assembledInputChars'] = len(json.dumps(assembled, ensure_ascii=False))
        except HTTPError as error:
            result.update(status='error', errorType='http', httpStatus=error.code)
        except (OSError, ValueError) as error:
            result.update(status='error', errorType=type(error).__name__)
        result['elapsedMs'] = round((time.monotonic() - started) * 1000)
        results.append(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(dict(schemaVersion='jev-upstream-probe/1', productionEnabled=False,
            replacesPlanning=False, endToEndSpeedVerified=False, questions=QUESTIONS, results=results), ensure_ascii=False, indent=2) + '\n')
        print(case['id'], result['status'], result['elapsedMs'])


if __name__ == '__main__':
    main()
