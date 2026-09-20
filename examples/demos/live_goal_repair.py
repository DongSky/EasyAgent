"""Bounded real-model repair of an incomplete moving comparison. No external writes."""
import asyncio
import json
from pathlib import Path

import httpx


async def main():
    workflow={'name':'搬家报价完整性核对','steps':[{'id':'comparison','kind':'transform','input':{'quotes':[{'company':'A','amount':100,'currency':'HKD'},{'company':'B','amount':150,'currency':'HKD'}]}}]}
    body={'objective':'现有两家搬家报价分别为 A 100 HKD、B 150 HKD。请返回两家报价及明确建议，计算两家的差价为 50 HKD，并保留币种。只使用给出的事实，不需要联网。将结果放入 comparison 节点输出：quotes、difference、currency、recommendation。','workflow':workflow,'model':'live-gpt','allowed_models':['live-gpt'],'allowed_tools':['core.to_text'],'semantic_check':False,'max_revisions':2,'limits':{'model_calls':3,'tool_calls':8,'output_tokens':24000,'wall_time_seconds':300},'checks':[{'description':'比较结果包含差价、币种与建议','path':'comparison','schema':{'type':'object','properties':{'difference':{'const':50},'currency':{'const':'HKD'},'recommendation':{'type':'string','minLength':1},'quotes':{'type':'array','minItems':2}},'required':['difference','currency','recommendation','quotes']}}]}
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8766',timeout=130) as c:
        response=await c.post('/v1/goals',json=body)
        response.raise_for_status()
        identifier=response.json()['id']
        print('goal',identifier,flush=True)
        async with asyncio.timeout(300):
            while True:
                r=await c.get('/v1/goals/'+identifier)
                r.raise_for_status()
                result=r.json()
                if result['run']['status'] in ('succeeded','failed','waiting_input','needs_attention','cancelled'):
                    Path('.eah/build/live-goal-repair.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
                    print('status',result['run']['status'],'revision',result['state'].get('revision'),'usage',result['run']['usage'])
                    assert result['run']['status']=='succeeded',result['run']['steps'][0].get('error') or result['state'].get('question')
                    break
                await asyncio.sleep(.5)

if __name__=='__main__':
    asyncio.run(main())
