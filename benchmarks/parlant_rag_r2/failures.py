"""Persist evidence-grounded qualitative notes; no scoring model or human-label fabrication."""
from common_r2 import *
def main():
    pairs=readl(RESULT/'CONTROL_vs_R2_answers.jsonl')
    categories={
      'A':{'definition':'检索有足够核心问题证据，生成仍误读或过度否认证据','examples':[
        {'sample':7,'task_id':pairs[6]['task_id'],'gold_recall5':1.0,'source_ids':['ibmcld_04105-1672-3877','ibmcld_04105-7-2225'],'evidence':'实际片段明确 bad bots breaking into user accounts to steal data 和 stealing user credentials。','CONTROL':'直接确认 some bad bots can obtain private information，并引用实际证据。','R2':'先称 documentation doesn’t directly answer / cannot give a definitive yes or no，随后复述同一偷数据证据。','interpretation':'R2过度保留，未将明确的窃取数据能力用于当前核心问题；不是缺检索证据。定性生成失败，不给事实准确率。'},
        {'sample':4,'task_id':pairs[3]['task_id'],'gold_recall5':0.0,'source_ids':['ibmcld_10852-44214-45420'],'evidence':'Top5 OpenWhisk sitemap 直接列 Install mobile SDK with Carthage，并链接 openwhisk-pkg_mobile_sdk 的安装章节。','CONTROL':'正确识别 Functions mobile SDK 的 Carthage 入口。','R2':'断言 I don’t have evidence confirming Carthage as an installation method for the IBM Cloud Functions mobile SDK，转向 Event Notifications。','interpretation':'虽qrels Recall为0，未标注 sitemap 仍支持“有该方法”的核心确认，R2忽略了它；不支持详细安装步骤，也不把sitemap替代缺失步骤证据。不能凭gold未命中就否定所有retrieval语义支持。'}]},
      'B':{'definition':'检索有核心证据，R2的表达范围或限定更贴合证据','examples':[
        {'sample':11,'task_id':pairs[10]['task_id'],'gold_recall5':0.5,'source_ids':['ibmcld_16410-8324-10312'],'evidence':'两人或以上共同标注是 inter-annotator agreement 的计算条件；三人仅为文档示例，非所有项目固定人数。','CONTROL':'回答 agreement 至少两人、示例三人。','R2':'新增限定 documentation doesn’t specify a fixed total number，同时给agreement至少两人的条件。','interpretation':'局部改善是问题范围/不确定性限定更明确，CONTROL原本核心条件也有支持。F1/ROUGE上升仅辅助，不证明事实准确率、显著性或整体收益。'}]},
      'C':{'definition':'目标证据缺失，证据包装无法补足检索','examples':[
        {'sample':1,'task_id':pairs[0]['task_id'],'gold_recall5':0.0,'source_ids':[e['document_id'] for e in pairs[0]['CONTROL']['actual_top5']],'evidence':'固定Top5没有reference对应的image tag X/Y/Z/date/hash证据。','CONTROL':'询问container registry/Kubernetes等语境。','R2':'询问part-of-speech/HTML等语境。','interpretation':'两组仍澄清，R2没有补齐原目标信息；包装不能造出缺失证据。Reference仅作评分侧检查，没有进入prompt。'}]},
      'D':{'definition':'实际Top5缺特定事实，模型仍给出该事实（可能来自历史）','examples':[
        {'sample':5,'task_id':pairs[4]['task_id'],'gold_recall5':0.0,'source_ids':[e['document_id'] for e in pairs[4]['CONTROL']['actual_top5']],'evidence':'实际Top5仅通用Lite quota、Discovery或Object Storage文档，没有Cloudant 1GB和吞吐配置。','CONTROL':'给20 reads、10 writes、5 queries、1GB及Standard吞吐/容量等具体数据。','R2':'虽说无直接推荐证据，仍给Cloudant fixed throughput、1GB cap和one Cloudant instance等事实。','interpretation':'这些内容可在既有官方历史找到，但历史在本轮仅用来理解问题，不是retrieved evidence事实来源；属于无本轮证据支持的新增断言风险，不自动等同世界事实错误，也不冒充人工标签。'}]}
    }
    rows=[]
    for cat,data in categories.items():
        for example in data['examples']:rows.append({'category':cat,'definition':data['definition'],**example,'review_type':'coding-assistant qualitative artifact inspection; not a separately invoked LLM judge or human label','human_verification':'unreviewed'})
    jsonl(RESULT/'failure_analysis.jsonl',rows)
    doc=['# R2 差异样本：A / B / C / D','','以下是当前编码助手对实际文本的定性检查，**不是人工标记、不是LLM judge运行、不是端到端事实准确率**。同一例可涉及检索缺失和证据使用问题。人工抽样的无证据新增事实标签另见 HUMAN_REVIEW_SHEET.md，目前 unreviewed。没有为了凑类别重跑样本或修改配置。','']
    for cat,data in categories.items():
        doc.extend([f"## {cat}. {data['definition']}",''])
        for ex in data['examples']:
            doc.extend([f"### {ex['sample']}. {ex['task_id']}",'',f"固定 qrels Recall@5={ex['gold_recall5']}；实际证据ID：{', '.join(ex['source_ids'])}。",'',ex['evidence'],'','CONTROL：'+ex['CONTROL'],'','R2：'+ex['R2'],'',ex['interpretation'],''])
    durable_text(REVIEW/'FAILURE_ANALYSIS.md','\n'.join(doc))
    print('A/B/C/D representative differences saved; human verification unreviewed')
if __name__=='__main__':main()
