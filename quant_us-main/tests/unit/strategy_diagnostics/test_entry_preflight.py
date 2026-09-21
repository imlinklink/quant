from scripts.strategy_diagnostics.entry_preflight import evaluate


def fixture(stuck):
    return ({'registration_id':'HA','study_basis':'S','study_manifest_hash':'hash',
             'maturity':{'common_candidate_cutoff':'2026-05-06'}},
            {'study_id':'S','manifest_hash':'hash'},
            {'baseline_parity':{'status':'VERIFIED'},'uncovered_by_parity':{
                'dividend_receivable_outstanding':{'stuck_within_window':stuck}}},
            [{'stage':'account_execution','candidate_round':'2026-04-30',
              'candidate_id':str(i),'result':r} for i,r in enumerate(['pass','reject'])])


def test_shared_accounting_defect_blocks_even_verified_parity():
    r=evaluate(*fixture(['2019-06-01']))
    assert r['status']=='ENGINEERING_BLOCKED'
    assert 'DIVIDENDS_STUCK_ON_NON_SESSION_PAY_DATES' in r['blockers']
    assert not r['returns_computed']


def test_clean_evidence_passes_without_dropping_rejected_opportunities():
    r=evaluate(*fixture([]))
    assert r['status']=='PREFLIGHT_PASSED'
    assert r['sample_inventory']=={'baseline_account_executed':1,'baseline_account_rejected':1,'baseline_ready':2}


def test_missing_accounting_audit_fails_closed():
    reg,study,checks,rows=fixture([])
    checks['uncovered_by_parity']={}
    assert 'DIVIDEND_SETTLEMENT_AUDIT_MISSING' in evaluate(reg,study,checks,rows)['blockers']
