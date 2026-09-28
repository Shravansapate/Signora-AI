"""Sanity tests for the evaluation itself; these are not application benchmarks."""
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'research_results/scripts'))
from analyse import canonical, represented, scores, stats
from dataset import make_dataset

def test_dataset_balance_and_ids():
    data=make_dataset()
    assert len(data)==224
    assert len({r['id'] for r in data})==224
    assert len({r['text'] for r in data})>=200
    assert all(sum(r['category']==category for r in data)==32 for category in {r['category'] for r in data})

def test_missing_statistics_are_not_zero():
    assert stats([]) is None
    assert stats([1,2,3])['mean']==2
    assert scores(1,1,1)['f1']==.5

def test_decode_actual_catalog_identity_and_partial_spelling():
    cat={'a':{'canonical_text':'a'},'t':{'canonical_text':'t'},'one':{'canonical_text':'1 one'}}
    trace=[{'motion_version_id':'a','method':'ALPHABET','gloss_token':'TRAIN','role':'event'}]
    assert 'TRAIN' not in represented(trace,cat)
    trace=[{'motion_version_id':'one','method':'DIGIT','gloss_token':'11','role':'entity'}]*2
    assert represented(trace,cat)=={'11':1}
    # A misleading match label cannot disguise an incorrect motion ID.
    trace=[{'motion_version_id':'a','method':'WORD','matched':'ISL_TRAIN_01','gloss_token':'TRAIN'}]
    assert represented(trace,cat)=={'A':1}
    assert canonical('2 two')=='2'
