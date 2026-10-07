"""Predeclared cases and deterministic visible-dialogue customer policy."""
import random, re

REQUEST_ID='11111111-1111-4111-8111-111111111111'
OP_ID='22222222-2222-4222-8222-222222222222'
REASON='尺寸不合适'
H='DEMO-1001-HEADSET';M='DEMO-1001-MUG';W='DEMO-1004-HEADSET'
SCENARIOS=[
 {'id':'Q1','group':'query','kind':'list','messages':['请列出我的全部订单编号和状态，不办理退货。']},
 {'id':'Q2','group':'query','kind':'details','messages':['查询订单 DEMO-1001 的商品明细、变体、各自数量和订单状态，不办理退货。']},
 {'id':'Q3','group':'query','kind':'request','messages':[f'查询退货申请 {REQUEST_ID}，告诉我申请编号、金额和当前状态，不办理新申请。']},
 {'id':'Q4','group':'query','kind':'operation','messages':['刚才退货成功了吗？请查询原操作状态，不要重新提交。'],'precondition':'one_unconfirmed_session_operation'},
]
for sid,item,qty,opening,staged in [
 ('R1',H,1,f'我要退订单 DEMO-1001 的明细 {H} 中1件，原因是{REASON}。请展示内容并等我确认。',[]),
 ('R2',M,1,f'我想退一个白色350ml陶瓷杯，原因是{REASON}。我没记住订单号，请先查我的订单和明细，等我确认再提交。',[]),
 ('R3',H,1,f'我想退黑色、4小时续航的无线耳机一件，原因是{REASON}。请查订单定位，等我确认。',[]),
 ('R4',H,2,f'订单 DEMO-1001 的 {H} 我要退2件，原因是{REASON}。请先展示等确认。',[]),
 ('R5',M,1,f'订单 DEMO-1001 有耳机、杯子和下载券，我只退 {M} 一件，其他商品不退，原因是{REASON}。先展示等确认。',[]),
 ('R6',H,1,f'我要退 DEMO-1001 的 {H} 一件，请先核对，退货原因稍后补充。',[f'退货原因是{REASON}，请准备数量1的内容，等我确认。']),
 ('R7',H,2,f'我要退 DEMO-1001 的 {H} 一件，原因是{REASON}，先展示等我确认。',[f'还没有确认，我把数量改成2件，原因仍是{REASON}。请重新准备2件的内容，1件的旧内容作废。']),
 ('R8',W,1,f'我要退 DEMO-1004 的白色无线耳机 {W} 一件，原因是{REASON}。等我明确确认再提交。',[]),
]:
 SCENARIOS.append({'id':sid,'group':'legal_return','kind':'return','item_id':item,'order_id':'DEMO-1004' if item==W else 'DEMO-1001','quantity':qty,'reason':REASON,'messages':[opening],'staged':staged})
for sid,oid,item,why in [('D1','DEMO-1002','DEMO-1002-KEYBOARD','pending'),('D2','DEMO-1003','DEMO-1003-MOUSE','expired'),('D3','DEMO-1001','DEMO-1001-DIGITAL','nonreturnable'),('D4','DEMO-2001','DEMO-2001-MUG','foreign')]:
 SCENARIOS.append({'id':sid,'group':'reject','kind':'reject','order_id':oid,'item_id':item,'quantity':1,'reason':REASON,'rejection':why,'messages':[f'我想退订单 {oid} 的明细 {item} 一件，原因是{REASON}。请检查是否能退，不能退时明确解释，不要提交。']})
SCENARIOS += [
 {'id':'B1','group':'boundary','kind':'ordinary_confirmation','item_id':H,'quantity':1,'reason':REASON,'messages':[f'退 DEMO-1001 的 {H} 一件，原因是{REASON}。先展示并等我确认。'],'staged':['确认']},
 {'id':'B2','group':'boundary','kind':'stale','item_id':H,'quantity':2,'reason':REASON,'messages':[f'退 DEMO-1001 的 {H} 一件，原因是{REASON}。先展示并等我确认。'],'staged':[f'数量改为2件，原因仍是{REASON}，旧1件申请作废，先展示新内容，暂不提交。'],'final':'旧口令引用仅为负向探测，不作为授权；不复制与现目标不符的口令提交'},
 {'id':'B3','group':'boundary','kind':'AB','item_id':H,'order_id':'DEMO-1001','quantity':1,'reason':REASON,'messages':[f'我要退 DEMO-1001 的 {H} 一件，原因是{REASON}，先展示等我确认。'],'after_submit':[f'A办理完后，我另想退 DEMO-1001 的 {M} 一件，原因是{REASON}。只准备B，我现在不确认也不提交。','刚才退货成功了吗？请查当前操作，不要重新提交。']},
 {'id':'B4','group':'boundary','kind':'followup','item_id':H,'order_id':'DEMO-1001','quantity':1,'reason':REASON,'messages':[f'我要退 DEMO-1001 的 {H} 一件，原因是{REASON}，先展示等我确认。'],'after_submit':[f'现在只查询 DEMO-1001 的 {M}，告诉我杯子变体和剩余数量，不再退杯子。','REPEAT_VISIBLE_CONFIRMATION']},
]
for s in SCENARIOS:
 if s['group']=='query':
  s['messages'].append('查询原操作状态，不要重新提交。' if s['kind']=='operation' else '请再确认上面的查询结果和状态，不办理任何新申请或退款。')
 elif s['group']=='reject':
  s['messages'].append('请确认这次请求没有提交新退货申请，也没有退款，并说明不能办理的原因。')
 s.setdefault('order_id','DEMO-1001')
 s['allowed_clarification']='仅根据既定目标补充订单/明细/数量/原因；无订单号题允许返回实际已查出的编号；选错商品纠正一次，仍不符失败；最多8条消息，不盲目确认'
 s['forbidden']='错误归属/错误商品/错误数量/错误原因/无真实确认申请；额外申请或数量占用；虚假退款；旧回执覆盖后续普通问答'
 s['end_condition']='预定脚本完成且处理屏障已结束，或8条/180秒单轮/720秒任务上限，或明确应用/基础设施故障'
 s['expected_business_state']='基线申请保留；'+ ('仅目标明细唯一新增申请、正确金额、确认关联、Outbox唯一回执' if s['kind'] in ('return','AB','followup') else '无新增申请/占用；允许只准备不确认的操作')
 assert len(SCENARIOS)==20

def plan():
 result=[]
 for repeat,seed in [(1,42),(2,43)]:
  cases=list(SCENARIOS);random.Random(seed).shuffle(cases)
  result += [{'attempt_id':f'r{repeat}_{s["id"]}','repeat':repeat,'seed':seed,'scenario':s} for s in cases]
 return result

PHRASE=re.compile(r'确认退货\s+([0-9a-fA-F-]{36})\s+订单([A-Za-z0-9-]+)\s+明细([A-Za-z0-9-]+)\s+数量([0-9]+)')

def visible_confirmation(text,goal,quantity=None):
 for m in reversed(list(PHRASE.finditer(text))):
  if m[2]==goal.get('order_id','DEMO-1001') and m[3]==goal['item_id'] and int(m[4])==(quantity or goal['quantity']) and goal['reason'] in text:
   return m[0]
 return None

def clarification(s):
 return f'请查询我的订单确定商品。我明确只退订单 {s["order_id"]} 的明细 {s["item_id"]}，数量{s["quantity"]}，原因是{s["reason"]}。先展示申请内容和完整口令，未确认不得提交；若已经提交原申请则不要再准备。'
