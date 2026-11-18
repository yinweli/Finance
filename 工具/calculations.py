import math
from operator import add, mul, sub, truediv


def positive(value):
    if value <= 0:
        raise ValueError("基準值須大於零")
    return value


def change(value, base):
    return 100 * (value - base) / positive(base)


def total_return(price, terminal, distribution):
    return change(terminal + distribution, price)


def annualized_return(price, terminal, distribution, years):
    gross = (terminal + distribution) / positive(price)
    if gross < 0:
        raise ValueError("期末總價值不可為負")
    return 100 * (gross ** (1 / positive(years)) - 1)


def bond_value(nav, duration, convexity, yield_change, fx, base_fx, exposed):
    if not 0 <= exposed <= 1:
        raise ValueError("外幣曝險比例須介於 0 與 1")
    rate_factor = 1 - duration * yield_change + convexity * yield_change ** 2 / 2
    fx_factor = 1 + exposed * (positive(fx) / positive(base_fx) - 1)
    return positive(nav) * positive(rate_factor) * fx_factor


def basket_earnings(*item):
    if len(item) < 2 or len(item) % 2:
        raise ValueError("整籃獲利參數須為持股數、每股盈餘的配對")
    return sum(shares * eps for shares, eps in zip(item[::2], item[1::2]))


OPERATION = {
    "add": add, "subtract": sub, "multiply": mul, "divide": truediv,
    "sum": lambda *item: sum(item),
    "premium": change, "growth": change, "fx": lambda amount, rate: amount * positive(rate),
    "allocation": lambda part, total: 100 * part / positive(total),
    "margin": lambda profit, revenue: 100 * profit / positive(revenue),
    "eps": lambda earnings, shares: earnings / positive(shares),
    "basket_earnings": basket_earnings,
    "basket_value": lambda earnings, multiple, other_assets: earnings * positive(multiple) + other_assets,
    "total_return": total_return, "annualized_return": annualized_return,
    "bond_value": bond_value,
    "cash_flow": lambda receipts, costs, capex: receipts - costs - capex,
}


def calculate(data):
    measure = data["input"] | data["assumption"]
    formula = data["formula"]
    result = {}
    pending = set()

    def resolve(key):
        if key in measure:
            return measure[key]["value"], measure[key].get("source", [])
        if key in result:
            return result[key]["value"], result[key]["source"]
        if key in pending:
            raise ValueError(f"公式循環引用 {key}")
        if key not in formula:
            raise ValueError(f"找不到數值 {key}")
        definition = formula[key]
        op = definition["op"]
        if op not in OPERATION:
            raise ValueError(f"未知公式 {op}")
        if data["asset_type"] == "bond_etf" and op in ("basket_earnings", "basket_value", "eps"):
            raise ValueError(f"bond_etf 不使用 {op}")
        if data["asset_type"] != "bond_etf" and op == "bond_value":
            raise ValueError("bond_value 僅適用 bond_etf")
        pending.add(key)
        arg = []
        source = set(definition.get("source", []))
        for reference in definition["args"]:
            value, evidence = resolve(reference) if isinstance(reference, str) else (reference, [])
            arg.append(value)
            source.update(evidence)
        try:
            value = None if None in arg else float(OPERATION[op](*arg))
            if value is not None and not math.isfinite(value):
                raise ValueError("結果須為有限數值")
        except (ArithmeticError, TypeError, ValueError) as error:
            raise ValueError(f"公式 {key} 無法計算：{error}") from error
        result[key] = {"value": value, "unit": definition["unit"], "source": sorted(source)}
        if "precision" in definition:
            result[key]["precision"] = definition["precision"]
        pending.remove(key)
        return value, source

    for key in formula:
        resolve(key)
    return result
