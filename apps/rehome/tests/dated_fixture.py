"""Synthetic dated metadata fixture; no native process or user profile."""
from pathlib import Path
import os
import native_adapter as native
import snapshot

def dated_fixture(fixture):
    with native._connect(fixture.profile/'state_5.sqlite') as db:
        ids=[r[0] for r in db.execute('SELECT id FROM threads ORDER BY id')]
        db.execute("INSERT INTO projects VALUES('empty-project','Empty project','{}',0,1700000000000,1700000200000)")
        db.execute('UPDATE projects SET position=5,created_at_ms=1700000300000,updated_at_ms=1700000400000 WHERE id=?',(fixture.pid,))
        db.execute("INSERT INTO thread_sections VALUES('dated-section','Dated section',NULL)")
        for index,tid in enumerate(ids):
            created=1700000000+index*100;updated=created+20
            values={'created_at':created,'updated_at':updated,'created_at_ms':None if index==0 else created*1000,
                'updated_at_ms':None if index==1 else updated*1000,'recency_at':updated,'recency_at_ms':updated*1000,
                'archived_at':updated if tid==fixture.fixture.child else None,'section_position':10-index,
                'section_entered_at_ms':created*1000+7}
            db.execute('UPDATE threads SET '+','.join(k+'=?' for k in values)+',thread_section_id=? WHERE id=?',
                       (*values.values(),'dated-section',tid))
            path=Path(db.execute('SELECT rollout_path FROM threads WHERE id=?',(tid,)).fetchone()[0])
            os.utime(path,ns=(updated*1000000000,updated*1000000000))
    state=snapshot._read_json(fixture.profile/'.codex-global-state.json')
    state['project-order']=['empty-project',fixture.pid]
    (fixture.profile/'.codex-global-state.json').write_text(native._json(state),encoding='utf-8')
    with native._connect(fixture.profile/'thread_history_1.sqlite') as db:
        db.execute('UPDATE thread_turns SET started_at=1700000000000+rollout_ordinal*100,completed_at=1700000000050+rollout_ordinal*100,duration_ms=50')
        db.execute('UPDATE thread_items SET created_at_ms=1700000000000+rollout_ordinal*100,started_at_ms=1700000000001+rollout_ordinal*100,completed_at_ms=1700000000002+rollout_ordinal*100')
    return ids

