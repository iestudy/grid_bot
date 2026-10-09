"""
net_inventory / cash_flow と実残高の整合性を、決定的に判定するモジュール。

背景: 2026-10-05と10-09、EMERGENCY_STOP後の自動対応が、正当な売り越し状態
(売り切ってJPYに変わった結果)を「帳簿と実残高の矛盾」と誤判定してエスカレー
ションし、botが長時間停止した。原因は、判定基準が注文サイズ
(buy_amount_per_level_xrp)に比例していたこと、かつClaudeがその値を知らず
推測で判断していたこと。整合性の判定は計算で決められるため、Claudeに任せず
コードで行う。

判定の前提:
  net_inventoryとcash_flowは、どちらもreset_state.py実行時点からの累積値。
  リセット時の実XRP残高(baseline)があれば、
    現在の実XRP ≒ baseline_xrp - net_inventory
  が成り立つ(売り越しなら net_inventory < 0 なので、実XRPは baseline 以上に
  増えるのではなく、baseline - |net_inventory| になる点に注意:
  net_inventory = 買い数量 - 売り数量。売り越し=負。実XRPの変化 = +買い -売
  = +net_inventory。よって 現在の実XRP ≒ baseline_xrp + net_inventory)。
"""
import re
from typing import Optional

# 平均約定価格が、現在価格からこの比率以内なら妥当とみなす。
AVG_PRICE_TOLERANCE_RATIO = 0.20

# 実XRPの変化の許容範囲: |net_inventory|のこの比率、または下の絶対値の大きい方。
# 手数料・丸めを見込んだ値。実データで調整が必要になる可能性がある。
XRP_DELTA_TOLERANCE_RATIO = 0.02
XRP_DELTA_TOLERANCE_ABS = 0.5

# net_inventoryがこの値未満(絶対値)なら、判定の意味がないので「整合」とみなす。
NEGLIGIBLE_INVENTORY_XRP = 0.5


def check_inventory_consistency(
    net_inventory: float,
    cash_flow: float,
    xrp_onhand: float,
    current_price: Optional[float],
    baseline_xrp: Optional[float],
) -> dict:
    """
    戻り値:
      {
        "status": "consistent" | "inconsistent" | "unknown",
        "reasons": [str, ...],      # 判定理由(人間が読める形)
        "avg_fill_price": float | None,
        "expected_xrp_onhand": float | None,
      }
    """
    reasons = []

    # net_inventoryがほぼゼロなら、帳簿と実残高の食い違いを検証する意味がない。
    if abs(net_inventory) < NEGLIGIBLE_INVENTORY_XRP:
        return {
            "status": "consistent",
            "reasons": [f"net_inventoryが{net_inventory:.4f}XRPとほぼゼロのため、検証対象外(整合とみなす)"],
            "avg_fill_price": None,
            "expected_xrp_onhand": None,
        }

    if current_price is None or current_price <= 0:
        return {
            "status": "unknown",
            "reasons": ["現在価格が取得できないため、関係1(平均約定価格)を検証できない"],
            "avg_fill_price": None,
            "expected_xrp_onhand": None,
        }

    # --- 関係1: cash_flowとnet_inventoryから平均約定価格を逆算し、現在価格と比べる ---
    # 売り越し(net_inventory<0): cash_flow>0(売って受け取ったJPY)。
    # 買い越し(net_inventory>0): cash_flow<0(買って支払ったJPY)。
    # どちらも cash_flow と net_inventory は逆符号。平均約定価格は -cash_flow/net_inventory で求める。
    avg_fill_price = -cash_flow / net_inventory
    relation1_ok = False
    if avg_fill_price <= 0:
        reasons.append(
            f"関係1: cash_flow({cash_flow:+.2f}円)とnet_inventory({net_inventory:+.4f}XRP)が"
            f"同符号で整合しない(売買の向きとJPYの動きが逆。平均約定価格={avg_fill_price:.2f}円が非正)"
        )
    else:
        deviation = abs(avg_fill_price - current_price) / current_price
        if deviation <= AVG_PRICE_TOLERANCE_RATIO:
            relation1_ok = True
            reasons.append(
                f"関係1: 平均約定価格{avg_fill_price:.2f}円は現在価格{current_price:.2f}円の"
                f"{deviation * 100:.1f}%以内で整合"
            )
        else:
            reasons.append(
                f"関係1: 平均約定価格{avg_fill_price:.2f}円が現在価格{current_price:.2f}円から"
                f"{deviation * 100:.1f}%乖離(許容{AVG_PRICE_TOLERANCE_RATIO * 100:.0f}%)"
            )

    # --- 関係2: リセット時の実XRP + net_inventory が、現在の実XRPと合うか ---
    if baseline_xrp is None:
        # 基準値がない場合、関係1だけを根拠に「整合」とは言わない。
        # 片方の関係だけでClaudeの判断を覆さないため。
        if not relation1_ok:
            return {
                "status": "inconsistent",
                "reasons": reasons + ["関係2: リセット時の実XRP残高(baseline)が記録されておらず検証不能"],
                "avg_fill_price": round(avg_fill_price, 4),
                "expected_xrp_onhand": None,
            }
        return {
            "status": "unknown",
            "reasons": reasons + ["関係2: リセット時の実XRP残高(baseline)が記録されておらず検証不能"],
            "avg_fill_price": round(avg_fill_price, 4),
            "expected_xrp_onhand": None,
        }

    expected_xrp_onhand = baseline_xrp + net_inventory
    tolerance = max(abs(net_inventory) * XRP_DELTA_TOLERANCE_RATIO, XRP_DELTA_TOLERANCE_ABS)
    diff = abs(xrp_onhand - expected_xrp_onhand)
    relation2_ok = diff <= tolerance
    if relation2_ok:
        reasons.append(
            f"関係2: 期待される実XRP({baseline_xrp:.4f}{net_inventory:+.4f}={expected_xrp_onhand:.4f})と"
            f"実際の実XRP({xrp_onhand:.4f})の差{diff:.4f}が許容範囲{tolerance:.4f}以内で整合"
        )
    else:
        reasons.append(
            f"関係2: 期待される実XRP({expected_xrp_onhand:.4f})と実際の実XRP({xrp_onhand:.4f})の"
            f"差{diff:.4f}が許容範囲{tolerance:.4f}を超える"
        )

    status = "consistent" if (relation1_ok and relation2_ok) else "inconsistent"
    return {
        "status": status,
        "reasons": reasons,
        "avg_fill_price": round(avg_fill_price, 4),
        "expected_xrp_onhand": round(expected_xrp_onhand, 4),
    }


def apply_consistency_override(decision: dict, incident_data: dict) -> dict:
    """
    コードによる在庫整合性判定が「consistent」で、Claudeがエスカレーション条件2
    だけを理由にエスカレーションを選んだ場合に限り、その判断を上書きする。

    背景: 2026-10-05と10-09、Claudeが正当な売り越し状態を条件2と誤判定して
    エスカレーションし、botが長時間停止した。整合性は計算で決められるため、
    コードの判定が整合を示すときは、Claudeの条件2の判断を採用しない。

    上書きの条件(すべて満たす場合のみ):
      1. inventory_consistency.status == "consistent"
      2. Claudeの action == "escalate"
      3. matched_conditionsから条件2を除いた残りが空
    条件2以外の条件も挙げられている場合は、actionを変えない(それらの条件は
    コードでは検証していないため)。

    戻り値: 新しいdecision(元のdictは変更しない)。
    """
    result = dict(decision)
    consistency = (incident_data or {}).get("inventory_consistency") or {}
    if consistency.get("status") != "consistent":
        return result
    if decision.get("action") != "escalate":
        return result

    conditions = list(decision.get("matched_conditions") or [])
    # 「条件2」を含む項目を、条件2に該当するものとして扱う。
    # (matched_conditionsは自由記述だが、これまでの出力はすべて「条件N: ...」の形式)
    # 「条件2」の直後が数字のもの(条件21など、将来の条件番号)は含めない。
    is_condition2 = lambda c: re.search(r"条件2(?!\d)", c) is not None
    condition2 = [c for c in conditions if is_condition2(c)]
    others = [c for c in conditions if not is_condition2(c)]

    code_reasons = "; ".join(consistency.get("reasons") or [])
    note = (
        "【コードによる在庫整合性判定: 整合】"
        f"{code_reasons}。"
    )

    if condition2 and not others:
        result["action"] = "auto_recover"
        result["matched_conditions"] = []
        result["reasoning"] = (
            f"{note}このため、Claudeが挙げた条件2は該当しないと扱い、判断を"
            f"エスカレーションから自動復旧に変更しました。"
            f"元のClaudeの判断: {decision.get('reasoning', '')}"
        )
    elif condition2:
        # 条件2以外も挙げられているため、actionは変えない。判断の経緯だけ追記する。
        result["reasoning"] = (
            f"{note}条件2は該当しないが、ほかの条件({'、'.join(others)})が"
            f"挙げられているためエスカレーションのままとします。"
            f"元のClaudeの判断: {decision.get('reasoning', '')}"
        )
    return result
