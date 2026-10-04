"""Persist explicit qualitative inspection notes, never a paid or accuracy judge."""
from common_holdout import *
def main():
    pairs=readl(RESULT/'R0_vs_R1B_answers.jsonl');rows=[]
    notes={
      1:('澄清而未解释 image tag；历史讨论 worker updates，当前短句仍有歧义。R0 已返回 X/Y/Z/date/hash 段但未用其解答，B 为可能的遗漏，不能断言澄清违反原规则。R1-B 检索偏向 Watson tag，标注证据全缺。','B_unverified', 'C_ambiguity_unverified'),
      2:('两组金标全缺，回答分别偏 Discovery relevancy training 和 Discovery Query parameters；reference 描述 Watson Query / API coordination，产品语境可能偏离。不能因引用在 Top5 就宣布回答正确。','B_unverified','C_unverified'),
      3:('reference 明说 documents do not provide an answer。R0 澄清代词，R1-B 说明 Lite 组合购买证据不足；与预期保守处理一致。此 C 根据 reference 和回答定性确认，非由无 qrels 推断。','B_not_identified','C_reference_supported'),
      4:('R0 返回并使用 Cloud Functions Carthage 安装片段。R1-B 金标缺失、检索到 Event Notifications Carthage；回答区分产品却未提供目标 SDK 的正确步骤，是检索回归。','B_unverified','C_retrieval_insufficient'),
      5:('两组金标均缺，回答使用既有历史中的 Cloudant 吞吐/容量信息，R0 无引用，R1-B 引用通用 Lite quota；与“历史仅用于理解问题”规则存在 grounding 风险。回答数字与 reference 接近不是证据使用正确的证明。','B_grounding_risk_unverified','C_retrieval_insufficient'),
      6:('两组金标全缺，但返还未标注 Watson Assistant conversation filtering 片段也可支持部分回答，不能把 qrels Recall=0 等同事实错误。reference 更强调 time period control，两组未覆盖该点。','B_unverified','C_product_ambiguity_unverified'),
      7:('两组 Recall@5=1。R0 直接肯定 malicious bots can steal credentials；R1-B 先复述 steal data 又说文档未说明 bots get private information in general，并引入 CIS anonymized JavaScript detection，混淆恶意 bot 能力与检测收集数据。这是证据存在后的过度保留/范围混淆案例；事实错误程度不作评分。','B_qualitative_scope_confusion_R1B','C_not_required_for_core_question'),
      8:('两组都仅澄清“VPC”，而 R1-B 已包含 VPC 定义和隔离网络片段。reference 要求解释 VPC；可能是生成过度澄清，但当前仅一个词且此前讨论 encryption，原规则允许澄清，B/C必要性未定。','B_unverified_overclarification','C_ambiguity_unverified'),
      9:('两组正确复述首人授权后 Active 7天及 Manager 第二授权，与实际 ibmcld_09061-1334-3188 一致；都没有 reference 的30/90天删除后窗口，但该内容超出当前直接问句，不能仅因 reference 更长判回答错误。','B_not_identified_for_core_question','C_not_required_for_core_question'),
      10:('两组金标全缺，reference 的 Welcome/Anything else 没有被覆盖。R0 说明不是完整列表；R1-B 给 standard/slots 分类，来自其实际段落。参考类型体系和检索文档类型体系不同，不能靠 ROUGE 宣布事实错误或正确。','B_unverified','C_R0_acknowledges_incomplete_evidence'),
      11:('R1-B 找到金标，回复“two or more human annotators annotate the same documents”与实际段落吻合；R0 金标缺失但未标注 tutorial 也支持至少两人和 overlap。可观察检索改善，不能据 gold ID 缺失抹去 R0 的证据支持。','B_not_identified_for_core_question','C_not_required_for_core_question')
    }
    for i,pair in enumerate(pairs,1):
        note,b,c=notes[i]
        for arm in ['R0','R1-B']:
            d=pair[arm]
            bstatus=b
            if i==7 and arm=='R0':bstatus='B_not_identified_for_core_question'
            rows.append({'sample_ordinal':i,'task_id':pair['task_id'],'candidate':arm,'A_qrels_diagnostic':d['retrieval_failure_A'],'A_missing_ids':d['gold_missing_from_top5'],'B_qualitative':bstatus,'C_qualitative':c,'inspection_note':note,'review_basis':'post-generation reference text + actual top5 + answer + official speaker/text history; no answerability labels, no LLM judge','factual_accuracy_score':'unavailable'})
    jsonl(RESULT/'failure_analysis.jsonl',rows)
    lines=['# Failure analysis: A / B / C','', '这是逐题定性检查，不是事实准确率评分。类别可同时出现；A 只描述标注证据覆盖，qrels 不保证所有可用证据都被标注。B 需要检查实际片段与回答，未确认时保留 unverified；C 区分 reference 明确无答案、检索不足、问题歧义，不把缺 qrels 当无答案。','', '11题中两组各有6题全部 qrels gold 缺于 Top5、3题部分缺、1题完整命中、1题qrels unavailable。此小样本与全 HOLDOUT 聚合改善不能混为同一结论。','', 'B 的具体观察：第7题 R1-B 返回且引用 bots breaking into user accounts to steal data，随后却说文档未说明一般 private information 能力，并转向 CIS 检测隐私；这是范围混淆/过度保留的定性诊断，未转成端到端正确率。第1/8题可能过度澄清，但原三条规则允许对歧义澄清，故不强判错。第5题有用历史做事实来源的风险，单列且不改规则。','', 'C 的明确案例：第3题两组都未直接回答 Lite 是否需搭售，reference 明确文档不给答案，拒答/澄清符合证据不足处理。第4题 R1-B 因检索错产品而缺目标 SDK 证据，属于 A 主因和 C 的运行状态。','']
    for i,p in enumerate(pairs,1):
        note,b,c=notes[i];lines.extend([f"## {i}. {p['task_id']}",'',f"R0 A={p['R0']['retrieval_failure_A']}；R1-B A={p['R1-B']['retrieval_failure_A']}。B={b}；C={c}。",'',note,''])
    (REVIEW/'FAILURE_ANALYSIS.md').write_text('\n'.join(lines))
    print('QUALITATIVE A/B/C NOTES SAVED; no accuracy score')
if __name__=='__main__':main()
