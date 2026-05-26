"""Same total deposits: $200/mo front-loaded vs $100/mo slower."""

years = 13


def fv_dca(init, monthly, months, ann, then_years=0.0):
    r = (1 + ann / 100) ** (1 / 12) - 1
    bal = init
    for _ in range(months):
        bal = bal * (1 + r) + monthly
    if then_years:
        bal *= (1 + ann / 100) ** then_years
    return bal


def pg(end, dep):
    return (end - dep) / dep * 100


plans = [
    ("100/mo full 13yr", 300, 100, 156, 0.0, 15900),
    ("200/mo first 6.5yr then stop", 300, 200, 78, 6.5, 15900),
    ("100/mo to 2030 then stop", 300, 100, 55, 13 - 55 / 12, 5800),
    ("200/mo ~28mo then stop (~same $)", 300, 200, 28, 13 - 28 / 12, 5900),
]

print("13-year horizon — same total $ in, faster vs slower\n")
header = f"{'Plan':<32} {'In':>7}  {'Low 3%':>8} {'Mid 10%':>9} {'Hi 18%':>9}  {'Mid %gain':>9}"
print(header)
print("-" * len(header))
for name, init, mo, m_contrib, y_after, dep in plans:
    ends = [fv_dca(init, mo, m_contrib, a, y_after) for a in (3, 10, 18)]
    print(
        f"{name:<32} ${dep:>5,}  ${ends[0]:>7,.0f} ${ends[1]:>8,.0f} ${ends[2]:>8,.0f}  +{pg(ends[1], dep):>7.1f}%"
    )

print("\nMid 10% — dollar difference (faster minus slower):")
slow15 = fv_dca(300, 100, 156, 10, 0)
fast15 = fv_dca(300, 200, 78, 10, 6.5)
print(f"  ~$15.9k total: 200/mo half beats 100/mo full by ${fast15 - slow15:+,.0f}  ({fast15:,.0f} vs {slow15:,.0f})")

slow6 = fv_dca(300, 100, 55, 10, 13 - 55 / 12)
fast6 = fv_dca(300, 200, 28, 10, 13 - 28 / 12)
print(f"  ~$5.8k total:  200/mo fast beats 100/mo to 2030 by ${fast6 - slow6:+,.0f}  ({fast6:,.0f} vs {slow6:,.0f})")
