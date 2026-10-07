import json, os, hashlib, time, signal, subprocess, socket, sys
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from psycopg import sql
from ..settings import ROOT, load_settings
from ..db import connect, initialize
from ..business import plain


def config():
    p = Path(os.environ.get('APP_EVAL_CONFIG', Path('runtime-data/retail-demo/app_s1/latest.txt').read_text().strip() + '/private.json'))
    return json.loads(p.read_text())


def save(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(plain(data),ensure_ascii=False,indent=2)+'\n');tmp.replace(path)


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def environment():
    c=config()
    return {**os.environ,'APP_EVAL_CONFIG':str(Path(c['runtime_dir'])/'private.json'),
      'DEMO_DATABASE_URL':f"postgresql://{c['user']}:{c['password']}@127.0.0.1:{c['db_port']}/{c['database']}",
      'DEMO_PARLANT_HOME':str(Path(c['result_dir'])/'parlant'),'PARLANT_HOME':str(Path(c['result_dir'])/'parlant'),
      'DEMO_CUSTOMER_ID':'demo-alice','DEMO_SESSION_STORAGE':'postgres',
      'DEMO_MCP_PORT':str(c['mcp_port']),'DEMO_PARLANT_PORT':str(c['parlant_port']),'DEMO_TOOL_PORT':str(c['tool_port'])}


def activate(): os.environ.update(environment())


def assert_isolated():
    c=config();s=load_settings()
    assert c['database']=='app_s1' and c['db_port']==55434
    assert s.database_url==environment()['DEMO_DATABASE_URL'] and s.parlant_port==8920 and s.mcp_port==8921
    with connect(s.database_url) as conn:
        assert conn.execute('SELECT current_database() AS d').fetchone()['d']=='app_s1'
    assert str(s.parlant_home).startswith(c['result_dir'])


TABLES=('customers','orders','order_items','return_operations','return_requests','operation_logs','session_operations','session_current_operations','tool_session_contexts','return_outbox','return_outbox_errors','outbox_control','parlant_sessions','parlant_events','parlant_customers','parlant_customer_tags','parlant_variables','parlant_variable_values','parlant_variable_tags','parlant_session_metadata','parlant_customer_metadata','parlant_variable_metadata','schema_migrations')


def database_evidence():
    assert_isolated()
    with connect(load_settings().database_url) as conn:
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        return plain({t:conn.execute(sql.SQL('SELECT * FROM {} ORDER BY 1').format(sql.Identifier(t))).fetchall() for t in TABLES})


@contextmanager
def service(module, folder, args=(), extra=None):
    c=config();port=c['mcp_port'] if module.endswith('mcp_server') else c['parlant_port']
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=.2): raise RuntimeError('Test port occupied')
    except OSError: pass
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    log=(folder/(module.split('.')[-1]+'.log')).open('a')
    env={**environment(),**(extra or {})}
    proc=subprocess.Popen([sys.executable,'-m',module,*args],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    save(Path(c['runtime_dir'])/(module.split('.')[-1]+'.process.json'),{'pid':proc.pid,'module':module,'port':port})
    try:
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            if proc.poll() is not None: raise RuntimeError('Test service exited; inspect isolated log')
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.2): break
            except OSError: time.sleep(.2)
        else: raise TimeoutError('Test startup timeout')
        yield proc
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid,signal.SIGTERM)
            try: proc.wait(20)
            except subprocess.TimeoutExpired: os.killpg(proc.pid,signal.SIGKILL);proc.wait()
        log.close()


def phase(attempt, category, turn=None):
    save(Path(config()['runtime_dir'])/'phase.json',{'attempt_id':attempt,'category':category,'turn':turn})
