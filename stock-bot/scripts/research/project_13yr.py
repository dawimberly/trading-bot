"""Quick 13-year VFIFX vs bot projection."""

months_13 = 13 * 12
months_to_2030 = 55
years = 13


def fv_lump(sum0, years, ann):
    return sum0 * (1 + ann / 100) ** years


def fv_dca(init, monthly, months, ann):
    r = (1 + ann / 100) ** (1 / 12) - 1
    bal = init
    for _ in range(months):
        bal = bal * (1 + r) + monthly
    return bal


def pct_gain(end, contributed):
    return (end - contributed) / contributed * 100


vfifx_start = 300_000
bot_init = 300
bot_mo = 100
contrib_all = bot_init + bot_mo * months_13
contrib_2030 = bot_init + bot_mo * months_to_2030

print("=== 13 YEARS (~2026 -> 2039) ===\n")
print("VFIFX: $300,000 locked, no new contributions\n")
for label, r in [("Low 5%/yr", 5), ("Mid 8%/yr", 8), ("High 10%/yr", 10)]:
    end = fv_lump(vfifx_start, years, r)
    pg = pct_gain(end, vfifx_start)
    print(f"  {label}: ${end:,.0f}  |  +{pg:.1f}% gain on $300k")

print("\nBot A: $300 + $100/mo for ALL 13 years")
print(f"  Total deposited: ${contrib_all:,}\n")
for label, r in [("Low 3%/yr", 3), ("Mid 10%/yr", 10), ("High 18%/yr", 18)]:
    end = fv_dca(bot_init, bot_mo, months_13, r)
    pg = pct_gain(end, contrib_all)
    print(f"  {label}: ${end:,.0f}  |  +{pg:.1f}% gain on deposits")

print("\nBot B: $300 + $100/mo until 2030, then compound to year 13")
print(f"  Total deposited: ${contrib_2030:,}\n")
for label, r in [("Low 3%/yr", 3), ("Mid 10%/yr", 10), ("High 18%/yr", 18)]:
    end_2030 = fv_dca(bot_init, bot_mo, months_to_2030, r)
    end = end_2030 * (1 + r / 100) ** (years - months_to_2030 / 12)
    pg = pct_gain(end, contrib_2030)
    print(f"  {label}: ${end:,.0f}  |  +{pg:.1f}% gain on deposits")

print("\n=== % GAIN COMPARISON (gain on money YOU put in) ===")
print(f"{'':28} {'Low':>10} {'Mid':>10} {'High':>10}")
print("-" * 60)
vf = [pct_gain(fv_lump(vfifx_start, years, r), vfifx_start) for r in (5, 8, 10)]
print(f"{'VFIFX ($300k)':28} {vf[0]:>9.0f}% {vf[1]:>9.0f}% {vf[2]:>9.0f}%")
ba = [pct_gain(fv_dca(bot_init, bot_mo, months_13, r), contrib_all) for r in (3, 10, 18)]
print(f"{'Bot $100/mo (13 yr)':28} {ba[0]:>9.0f}% {ba[1]:>9.0f}% {ba[2]:>9.0f}%")
bb = []
for r in (3, 10, 18):
    e2030 = fv_dca(bot_init, bot_mo, months_to_2030, r)
    end = e2030 * (1 + r / 100) ** (years - months_to_2030 / 12)
    bb.append(pct_gain(end, contrib_2030))
print(f"{'Bot $100/mo (to 2030)':28} {bb[0]:>9.0f}% {bb[1]:>9.0f}% {bb[2]:>9.0f}%")

print("\n=== DOLLARS (mid case: VFIFX 8%, bot 10%) ===")
v_end = fv_lump(vfifx_start, years, 8)
b_end = fv_dca(bot_init, bot_mo, months_13, 10)
print(f"VFIFX: ${v_end:,.0f}  (+${v_end - vfifx_start:,.0f})")
print(f"Bot:   ${b_end:,.0f}  (+${b_end - contrib_all:,.0f} on ${contrib_all:,} in)")
