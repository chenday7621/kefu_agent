"""Identical isolated business fixture; resets only verified APP-EVAL database."""
from datetime import datetime, timedelta, timezone
from psycopg.types.json import Jsonb
from ..db import connect
from ..settings import load_settings
from .common import assert_isolated, config
from .protocol import REQUEST_ID,OP_ID


def reset():
 assert_isolated()
 with connect(load_settings().database_url) as c:
  c.execute('TRUNCATE orders,order_items,return_operations,return_requests,operation_logs,session_operations,session_current_operations,tool_session_contexts,return_outbox,return_outbox_errors RESTART IDENTITY CASCADE')
  c.execute('UPDATE outbox_control SET enabled=true')
  c.execute("INSERT INTO customers VALUES ('demo-alice','演示客户 Alice'),('demo-bob','演示客户 Bob') ON CONFLICT DO NOTHING")
  # One fixed date anchor for both repetitions, not a reset-dependent moving date.
  anchor=datetime.fromisoformat(config()['fixture_anchor'])
  for oid,customer,status,days in [('DEMO-0999','demo-alice','delivered',5),('DEMO-1001','demo-alice','delivered',3),('DEMO-1002','demo-alice','pending',None),('DEMO-1003','demo-alice','delivered',45),('DEMO-1004','demo-alice','delivered',2),('DEMO-2001','demo-bob','delivered',2)]:
   c.execute('INSERT INTO orders(id,customer_id,status,created_at,delivered_at) VALUES(%s,%s,%s,%s,%s)',(oid,customer,status,anchor-timedelta(days=days or 1),anchor-timedelta(days=days) if days else None))
  for row in [('DEMO-0999-MIC','DEMO-0999','麦克风','黑色',1,5900,True),('DEMO-1001-HEADSET','DEMO-1001','无线耳机','黑色 / 4小时续航',3,12900,True),('DEMO-1001-MUG','DEMO-1001','陶瓷杯','白色 / 350ml',2,3900,True),('DEMO-1001-DIGITAL','DEMO-1001','数字下载券','电子交付',1,1900,False),('DEMO-1002-KEYBOARD','DEMO-1002','机械键盘','有线 / 白色',1,25900,True),('DEMO-1003-MOUSE','DEMO-1003','鼠标','黑色',1,8900,True),('DEMO-1004-HEADSET','DEMO-1004','无线耳机','白色 / 8小时续航',2,14900,True),('DEMO-2001-MUG','DEMO-2001','陶瓷杯','蓝色',2,3900,True)]:
   c.execute('INSERT INTO order_items(id,order_id,product_name,variant,quantity,unit_price_cents,returnable) VALUES(%s,%s,%s,%s,%s,%s,%s)',row)
  c.execute("INSERT INTO return_operations(id,customer_id,order_id,item_id,quantity,reason,quoted_unit_price_cents,created_at,confirmed_at,confirmation_source) VALUES(%s,'demo-alice','DEMO-0999','DEMO-0999-MIC',1,'演示历史申请',5900,%s,%s,'app-eval-initial-fixture')",(OP_ID,anchor,anchor))
  c.execute("INSERT INTO return_requests(id,operation_id,customer_id,order_id,item_id,quantity,reason,amount_cents,created_at) VALUES(%s,%s,'demo-alice','DEMO-0999','DEMO-0999-MIC',1,'演示历史申请',5900,%s)",(REQUEST_ID,OP_ID,anchor))
  for action in ('prepared','confirmed','submitted'):
   c.execute("INSERT INTO operation_logs(operation_id,customer_id,action,details,created_at) VALUES(%s,'demo-alice',%s,%s,%s)",(OP_ID,action,Jsonb({'fixture':True}),anchor))
