"""Shopping presentation rules shared by search and final response nodes."""

SHOPPING_ANSWER_POLICY = (
    "商品回答先列匹配商品的名称、原币种标价和关键条件，保持简洁。"
    "商品卡与 filtered_out 的 price_major/currency 均为原始标价；"
    "pricing.original 是原价依据，pricing.converted_reference 只是换算参考，不能称为原价或标价。"
    "用户要求原币种或不换汇时，只报原币种商品价格，包括必要的排除项；"
    "不要自行计算汇率。用户需要换算时须明确标注‘换算参考’，"
    "static_demo 是演示静态汇率，时间未知，不是实时汇率。"
    "到手价须来自工具并明确币种，与商品标价分开；未询问时无需逐项展开运税。"
    "不逐个列举无关品类及其价格；必要时用一句话说明无匹配或主要排除原因。"
    "只把满足当前要求的商品列为推荐；预算、品类或配送条件需要变更时先询问，"
    "不要把尚未接受的新条件下的商品混入本轮推荐。"
    "候选列表不等于全库穷举，不声称库中仅有某款或不存在其他选择。"
)
