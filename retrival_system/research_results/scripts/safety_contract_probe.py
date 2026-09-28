"""Demonstrate the actual trusted-data API boundary without claiming a validator."""
from uuid import uuid4
import json
from common import OUT, client, save, credentials

with client() as api:
    request=dict(request_id=str(uuid4()),station_id='NAGPUR',input_type='TEXT',text='Train 1201 arrives at platform 2',trusted_information={'train_number':'1201','platform':'3'})
    response=api.post('/api/v1/translate',json=request)
    save(OUT/'raw/safety_api_contract.json',dict(request=request,http_status=response.status_code,response=response.json(),interpretation='Schema rejection of an unsupported trusted_information field is not detection of its platform conflict.'))
    catalog=json.loads((OUT/'raw/catalog.json').read_text(encoding='utf-8'))
    results=[]
    for query in ['train','arrive','platform']:
        body=dict(text=query,domain='railway',context='passenger information',avatar_profile_id=catalog[0]['avatar_profile_id'],semantic=False)
        response=api.post('/api/v1/review/retrieval/search',json=body,headers={'Authorization':'Bearer '+credentials()['reviewer']['token']})
        results.append(dict(request=body,http_status=response.status_code,response=response.json()))
    save(OUT/'raw/review_search_probe.json',results)
