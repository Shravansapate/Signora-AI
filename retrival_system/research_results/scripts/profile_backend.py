"""Real translate path with scoped timing wrappers and rolled-back preview records."""
import hashlib
import time
from collections import defaultdict
from datetime import date
from uuid import uuid4
from common import OUT, append, credentials, records, settings
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from app import announcements, demo, meaning
from app.announcement_schema import AnnouncementInput
from app.storage import LocalAssetStore

def main():
    cfg=settings();engine=create_engine(cfg.database_url.get_secret_value());store=LocalAssetStore(cfg.storage_root)
    identity=cfg.principals[hashlib.sha256(credentials()['operator']['token'].encode()).hexdigest()]
    measurements=defaultdict(float);original=[]
    def wrap(module,name,label):
        function=getattr(module,name);original.append((module,name,function))
        def timed(*a,**kw):
            t=time.perf_counter_ns()
            try:return function(*a,**kw)
            finally:measurements[label]+=(time.perf_counter_ns()-t)/1e6
        setattr(module,name,timed)
    for module,name,label in [(announcements,'parse_meaning','parse_combined_ms'),(announcements,'construct_gloss','gloss_ms'),(demo,'_select','selector_ms'),(demo,'_check_bytes','asset_integrity_ms'),(demo,'_persist','manifest_persist_ms'),(meaning,'normalize','normalize_inclusive_ms'),(meaning,'_slot','entities_validation_inclusive_ms')]:wrap(module,name,label)
    @event.listens_for(engine,'before_cursor_execute')
    def before(conn,cursor,statement,parameters,context,executemany):context.eval_started=time.perf_counter_ns()
    @event.listens_for(engine,'after_cursor_execute')
    def after(conn,cursor,statement,parameters,context,executemany):measurements['sql_execute_ms']+=(time.perf_counter_ns()-context.eval_started)/1e6
    try:
        path=OUT/'raw/backend_profile.jsonl';done={r['trial'] for r in records(path)}
        for trial in range(20):
            if trial in done:continue
            with engine.connect() as connection:
                transaction=connection.begin()
                try:
                    with Session(bind=connection,join_transaction_mode='create_savepoint') as session:
                        request=AnnouncementInput(request_id=uuid4(),station_id='NAGPUR',input_type='TEXT',text='Train 1201 arrives at platform 2',service_date=date(2026,9,27))
                        measurements.clear();t=time.perf_counter_ns()
                        result=announcements.translate(session,store,request,identity,demo_mode_enabled=True)
                        elapsed=(time.perf_counter_ns()-t)/1e6
                        append(path,dict(trial=trial,backend_total_ms=elapsed,clips=len(result.manifest.items) if result.manifest else 0,status=result.status,**measurements,scope='Actual backend function; inclusive stage wrappers overlap; outer transaction rolls back; excludes HTTP and browser'))
                finally:transaction.rollback()
    finally:
        for module,name,function in original:setattr(module,name,function)
    print('20 real backend traces saved; preview inserts rolled back.')

if __name__=='__main__':main()
