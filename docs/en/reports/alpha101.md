# Alpha-101 Test Report

**Source:** Kakushadze (2016), *101 Formulaic Alphas*, arXiv:1601.00991  
**Engine:** `assay.engine` (numpy `(T×N)` backend) · **Catalog:** `assay.factors.alpha101`  
**Panel:** synthetic, 420 trading days × 30 symbols, continuous dynamics, fixed seed  
**Groups:** sector/industry/subindustry labels supplied for `indneutralize` alphas

## Summary

| Metric | Result |
|---|---|
| pytest (`tests/test_alpha101.py`) | **210 passed** |
| Alphas parsed → unified AST | **101 / 101** |
| Alphas evaluated (no error, shape 420×30) | **101 / 101** |
| Adversarial faithfulness audit (202 agents) | **101 / 101 faithful** |
| Produced finite values on synthetic panel | **99 / 101** |
| All-NaN on synthetic panel (deep chains; resolve on real data) | [96, 97] |
| `indneutralize` alphas (need group_data) | 18: [48, 58, 59, 63, 67, 69, 70, 76, 79, 80, 82, 87, 89, 90, 91, 93, 97, 100] |
| Mean coverage (finite fraction, finite alphas) | 82.1% |

## Operator usage across the 101 alphas

| operator | category | # alphas |
|---|---|---|
| `mul` | arithmetic | 85 |
| `cs_rank` | cross-sectional | 77 |
| `sub` | arithmetic | 67 |
| `ts_corr` | time-series | 58 |
| `ts_mean` | time-series | 45 |
| `div` | arithmetic | 44 |
| `add` | arithmetic | 44 |
| `ts_rank` | time-series | 38 |
| `ts_delta` | time-series | 35 |
| `ts_sum` | time-series | 30 |
| `lt` | comparison | 24 |
| `ts_decay_linear` | time-series | 22 |
| `cs_neutralize` | cross-sectional | 18 |
| `ts_delay` | time-series | 17 |
| `pow` | math | 15 |
| `ts_returns` | time-series | 12 |
| `ts_min` | time-series | 12 |
| `where` | math | 11 |
| `ts_max` | time-series | 9 |
| `ts_std` | time-series | 6 |
| `sign` | math | 6 |
| `cs_scale` | cross-sectional | 6 |
| `abs` | math | 5 |
| `elem_max` | math | 5 |
| `ts_argmax` | time-series | 4 |
| `elem_min` | math | 4 |
| `log` | math | 3 |
| `signed_power` | math | 2 |
| `ts_cov` | time-series | 2 |
| `eq` | comparison | 2 |
| `or` | logical | 2 |
| `ts_product` | time-series | 2 |
| `ts_argmin` | time-series | 2 |

## Per-alpha detail

| # | ops | fields | group? | coverage | warm-up row | expression |
|---:|---:|---:|:--:|---:|---:|---|
| 1 | 8 | 1 |  | 95% | 12 | `(rank(Ts_ArgMax(SignedPower(((returns < 0) ? stddev(returns, 20) ...` |
| 2 | 7 | 3 |  | 98% | 7 | `(-1 * correlation(rank(delta(log(volume), 2)), rank(((close - ope...` |
| 3 | 3 | 2 |  | 91% | 9 | `(-1 * correlation(rank(open), rank(volume), 10))` |
| 4 | 3 | 1 |  | 98% | 8 | `(-1 * Ts_Rank(rank(low), 9))` |
| 5 | 6 | 3 |  | 98% | 9 | `(rank((open - (sum(vwap, 10) / 10))) * (-1 * abs(rank((close - vw...` |
| 6 | 2 | 2 |  | 98% | 9 | `(-1 * correlation(open, volume, 10))` |
| 7 | 8 | 2 |  | 90% | 19 | `((adv20 < volume) ? ((-1 * ts_rank(abs(delta(close, 7)), 60)) * s...` |
| 8 | 6 | 2 |  | 96% | 15 | `(-1 * rank(((sum(open, 5) * sum(returns, 5)) - delay((sum(open, 5...` |
| 9 | 6 | 1 |  | 99% | 5 | `((0 < ts_min(delta(close, 1), 5)) ? delta(close, 1) : ((ts_max(de...` |
| 10 | 7 | 1 |  | 99% | 4 | `rank(((0 < ts_min(delta(close, 1), 4)) ? delta(close, 1) : ((ts_m...` |
| 11 | 7 | 3 |  | 99% | 3 | `((rank(ts_max((vwap - close), 3)) + rank(ts_min((vwap - close), 3...` |
| 12 | 3 | 2 |  | 100% | 1 | `(sign(delta(volume, 1)) * (-1 * delta(close, 1)))` |
| 13 | 3 | 2 |  | 99% | 4 | `(-1 * rank(covariance(rank(close), rank(volume), 5)))` |
| 14 | 5 | 3 |  | 98% | 9 | `((-1 * rank(delta(returns, 3))) * correlation(open, volume, 10))` |
| 15 | 4 | 2 |  | 26% | 4 | `(-1 * sum(rank(correlation(rank(high), rank(volume), 3)), 3))` |
| 16 | 3 | 2 |  | 99% | 4 | `(-1 * rank(covariance(rank(high), rank(volume), 5)))` |
| 17 | 6 | 2 |  | 95% | 23 | `(((-1 * rank(ts_rank(close, 10))) * rank(delta(delta(close, 1), 1...` |
| 18 | 7 | 2 |  | 98% | 9 | `(-1 * rank(((stddev(abs((close - open)), 5) + (close - open)) + c...` |
| 19 | 9 | 1 |  | 40% | 250 | `((-1 * sign(((close - delay(close, 7)) + delta(close, 7)))) * (1 ...` |
| 20 | 4 | 4 |  | 100% | 1 | `(((-1 * rank((open - delay(high, 1)))) * rank((open - delay(close...` |
| 21 | 11 | 2 |  | 97% | 7 | `((((sum(close, 8) / 8) + stddev(close, 8)) < (sum(close, 2) / 2))...` |
| 22 | 5 | 3 |  | 95% | 19 | `(-1 * (delta(correlation(high, volume, 5), 5) * rank(stddev(close...` |
| 23 | 6 | 1 |  | 95% | 19 | `(((sum(high, 20) / 20) < high) ? (-1 * delta(high, 2)) : 0)` |
| 24 | 11 | 1 |  | 53% | 199 | `((((delta((sum(close, 100) / 100), 100) / delay(close, 100)) < 0....` |
| 25 | 5 | 4 |  | 95% | 19 | `rank(((((-1 * returns) * adv20) * vwap) * (high - close)))` |
| 26 | 4 | 2 |  | 85% | 10 | `(-1 * ts_max(correlation(ts_rank(volume, 5), ts_rank(high, 5), 5)...` |
| 27 | 7 | 2 |  | 69% | 6 | `((0.5 < rank((sum(correlation(rank(volume), rank(vwap), 6), 2) / ...` |
| 28 | 6 | 4 |  | 95% | 23 | `scale(((correlation(adv20, low, 5) + ((high + low) / 2)) - close))` |
| 29 | 13 | 1 |  | 86% | 11 | `(min(product(rank(rank(scale(log(sum(ts_min(rank(rank((-1 * rank(...` |
| 30 | 8 | 2 |  | 95% | 19 | `(((1.0 - rank(((sign((close - delay(close, 1))) + sign((delay(clo...` |
| 31 | 9 | 3 |  | 93% | 30 | `((rank(rank(rank(decay_linear((-1 * rank(rank(delta(close, 10))))...` |
| 32 | 8 | 2 |  | 44% | 234 | `(scale(((sum(close, 7) / 7) - close)) + (20 * scale(correlation(v...` |
| 33 | 5 | 2 |  | 100% | 0 | `rank((-1 * ((1 - (open / close))^1)))` |
| 34 | 7 | 1 |  | 99% | 5 | `rank(((1 - rank((stddev(returns, 2) / stddev(returns, 5)))) + (1 ...` |
| 35 | 5 | 4 |  | 92% | 32 | `((Ts_Rank(volume, 32) * (1 - Ts_Rank(((close + high) - low), 16))...` |
| 36 | 12 | 4 |  | 53% | 199 | `(((((2.21 * rank(correlation((close - open), delay(volume, 1), 15...` |
| 37 | 5 | 2 |  | 52% | 200 | `(rank(correlation(delay((open - close), 1), close, 200)) + rank((...` |
| 38 | 4 | 2 |  | 98% | 9 | `((-1 * rank(Ts_Rank(close, 10))) * rank((close / open)))` |
| 39 | 10 | 2 |  | 40% | 250 | `((-1 * rank((delta(close, 7) * (1 - rank(decay_linear((volume / a...` |
| 40 | 4 | 2 |  | 98% | 9 | `((-1 * rank(stddev(high, 10))) * correlation(high, volume, 10))` |
| 41 | 3 | 3 |  | 100% | 0 | `(((high * low)^0.5) - vwap)` |
| 42 | 4 | 2 |  | 97% | 0 | `(rank((vwap - close)) / rank((vwap + close)))` |
| 43 | 5 | 2 |  | 91% | 38 | `(ts_rank((volume / adv20), 20) * ts_rank((-1 * delta(close, 7)), 8))` |
| 44 | 3 | 2 |  | 99% | 4 | `(-1 * correlation(high, rank(volume), 5))` |
| 45 | 6 | 2 |  | 94% | 24 | `(-1 * ((rank((sum(delay(close, 5), 20) / 20)) * correlation(close...` |
| 46 | 6 | 1 |  | 95% | 20 | `((0.25 < (((delay(close, 20) - delay(close, 10)) / 10) - ((delay(...` |
| 47 | 7 | 4 |  | 95% | 19 | `((((rank((1 / close)) * volume) / adv20) * ((high * rank((high - ...` |
| 48 | 8 | 1 | Y | 40% | 251 | `(indneutralize(((correlation(delta(close, 1), delta(delay(close, ...` |
| 49 | 6 | 1 |  | 95% | 20 | `(((((delay(close, 20) - delay(close, 10)) / 10) - ((delay(close, ...` |
| 50 | 4 | 2 |  | 52% | 8 | `(-1 * ts_max(rank(correlation(rank(volume), rank(vwap), 5)), 5))` |
| 51 | 6 | 1 |  | 95% | 20 | `(((((delay(close, 20) - delay(close, 10)) / 10) - ((delay(close, ...` |
| 52 | 10 | 3 |  | 43% | 240 | `((((-1 * ts_min(low, 5)) + delay(ts_min(low, 5), 5)) * rank(((sum...` |
| 53 | 4 | 3 |  | 98% | 9 | `(-1 * delta((((close - low) - (high - close)) / (close - low)), 9))` |
| 54 | 4 | 4 |  | 100% | 0 | `((-1 * ((low - close) * (open^5))) / ((low - high) * (close^5)))` |
| 55 | 7 | 4 |  | 96% | 16 | `(-1 * correlation(rank(((close - ts_min(low, 12)) / (ts_max(high,...` |
| 56 | 6 | 2 |  | 98% | 10 | `(0 - (1 * (rank((sum(returns, 10) / sum(sum(returns, 2), 3))) * r...` |
| 57 | 6 | 2 |  | 93% | 30 | `(0 - (1 * ((close - vwap) / decay_linear(rank(ts_argmax(close, 30...` |
| 58 | 5 | 2 | Y | 97% | 12 | `(-1 * Ts_Rank(decay_linear(correlation(IndNeutralize(vwap, IndCla...` |
| 59 | 7 | 2 | Y | 94% | 25 | `(-1 * Ts_Rank(decay_linear(correlation(IndNeutralize(((vwap * 0.7...` |
| 60 | 6 | 4 |  | 98% | 9 | `(0 - (1 * ((2 * scale(rank(((((close - low) - (high - close)) / (...` |
| 61 | 6 | 2 |  | 54% | 195 | `(rank((vwap - ts_min(vwap, 16.1219))) < rank(correlation(vwap, ad...` |
| 62 | 8 | 5 |  | 89% | 48 | `((rank(correlation(vwap, sum(adv20, 22.4101), 9.91009)) < rank(((...` |
| 63 | 10 | 4 | Y | 43% | 238 | `((rank(decay_linear(delta(IndNeutralize(close, IndClass.industry)...` |
| 64 | 10 | 5 |  | 65% | 145 | `((rank(correlation(sum(((open * 0.178404) + (low * (1 - 0.178404)...` |
| 65 | 9 | 3 |  | 83% | 71 | `((rank(correlation(((open * 0.00817205) + (vwap * (1 - 0.00817205...` |
| 66 | 8 | 4 |  | 96% | 15 | `((rank(decay_linear(delta(vwap, 3.51013), 7.23052)) + Ts_Rank(dec...` |
| 67 | 8 | 3 | Y | 94% | 1 | `((rank((high - ts_min(high, 2.14593)))^rank(correlation(IndNeutra...` |
| 68 | 9 | 4 |  | 57% | 33 | `((Ts_Rank(correlation(rank(high), rank(adv15), 8.91644), 13.9333)...` |
| 69 | 11 | 3 | Y | 93% | 5 | `((rank(ts_max(delta(IndNeutralize(vwap, IndClass.industry), 2.724...` |
| 70 | 8 | 3 | Y | 81% | 1 | `((rank(delta(vwap, 1.29456))^Ts_Rank(correlation(IndNeutralize(cl...` |
| 71 | 9 | 5 |  | 7% | 224 | `max(Ts_Rank(decay_linear(correlation(Ts_Rank(close, 3.43976), Ts_...` |
| 72 | 7 | 4 |  | 78% | 55 | `(rank(decay_linear(correlation(((high + low) / 2), adv40, 8.93345...` |
| 73 | 9 | 3 |  | 95% | 19 | `(max(rank(decay_linear(delta(vwap, 4.72775), 2.91864)), Ts_Rank(d...` |
| 74 | 8 | 4 |  | 74% | 79 | `((rank(correlation(close, sum(adv30, 37.4843), 15.1365)) < rank(c...` |
| 75 | 4 | 3 |  | 79% | 60 | `(rank(correlation(vwap, volume, 4.24304)) < rank(correlation(rank...` |
| 76 | 9 | 3 | Y | 67% | 139 | `(max(rank(decay_linear(delta(vwap, 1.24383), 11.8259)), Ts_Rank(d...` |
| 77 | 8 | 4 |  | 89% | 45 | `min(rank(decay_linear(((((high + low) / 2) + high) - (vwap + high...` |
| 78 | 8 | 3 |  | 57% | 4 | `(rank(correlation(sum(((low * 0.352233) + (vwap * (1 - 0.352233))...` |
| 79 | 10 | 4 | Y | 30% | 170 | `(rank(delta(IndNeutralize(((close * 0.60733) + (open * (1 - 0.607...` |
| 80 | 11 | 3 | Y | 96% | 17 | `((rank(Sign(delta(IndNeutralize(((open * 0.868128) + (high * (1 -...` |
| 81 | 9 | 2 |  | 41% | 77 | `((rank(Log(product(rank((rank(correlation(vwap, sum(adv10, 49.605...` |
| 82 | 10 | 2 | Y | 92% | 33 | `(min(rank(decay_linear(delta(open, 1.46063), 14.8717)), Ts_Rank(d...` |
| 83 | 6 | 5 |  | 99% | 6 | `((rank(delay(((high - low) / (sum(close, 5) / 5)), 2)) * rank(ran...` |
| 84 | 5 | 2 |  | 82% | 33 | `SignedPower(Ts_Rank((vwap - ts_max(vwap, 15.3217)), 20.7127), del...` |
| 85 | 9 | 4 |  | 90% | 15 | `(rank(correlation(((high * 0.876703) + (close * (1 - 0.876703))),...` |
| 86 | 9 | 4 |  | 87% | 56 | `((Ts_Rank(correlation(close, sum(adv20, 14.7444), 6.00049), 20.41...` |
| 87 | 12 | 3 | Y | 74% | 108 | `(max(rank(decay_linear(delta(((close * 0.369701) + (vwap * (1 - 0...` |
| 88 | 8 | 5 |  | 25% | 91 | `min(rank(decay_linear(((rank(open) + rank(low)) - (rank(high) + r...` |
| 89 | 9 | 3 | Y | 94% | 26 | `(Ts_Rank(decay_linear(correlation(((low * 0.967285) + (low * (1 -...` |
| 90 | 9 | 3 | Y | 89% | 45 | `((rank((close - ts_max(close, 4.66719)))^Ts_Rank(correlation(IndN...` |
| 91 | 8 | 3 | Y | 92% | 33 | `((Ts_Rank(decay_linear(decay_linear(correlation(IndNeutralize(clo...` |
| 92 | 9 | 5 |  | 60% | 45 | `min(Ts_Rank(decay_linear(((((high + low) / 2) + close) < (low + o...` |
| 93 | 11 | 3 | Y | 69% | 120 | `(Ts_Rank(decay_linear(correlation(IndNeutralize(vwap, IndClass.in...` |
| 94 | 8 | 2 |  | 47% | 10 | `((rank((vwap - ts_min(vwap, 11.5783)))^Ts_Rank(correlation(Ts_Ran...` |
| 95 | 11 | 4 |  | 81% | 78 | `(rank((open - ts_min(open, 12.4105))) < Ts_Rank((rank(correlation...` |
| 96 | 8 | 3 |  | 0% | — | `(max(Ts_Rank(decay_linear(correlation(rank(vwap), rank(volume), 3...` |
| 97 | 10 | 3 | Y | 0% | — | `((rank(decay_linear(delta(IndNeutralize(((low * 0.721001) + (vwap...` |
| 98 | 8 | 3 |  | 80% | 52 | `(rank(decay_linear(correlation(vwap, sum(adv5, 26.4719), 4.58418)...` |
| 99 | 8 | 3 |  | 80% | 84 | `((rank(correlation(sum(((high + low) / 2), 19.8975), sum(adv60, 1...` |
| 100 | 9 | 4 | Y | 92% | 29 | `(0 - (1 * (((1.5 * scale(indneutralize(indneutralize(rank(((((clo...` |
| 101 | 3 | 4 |  | 100% | 0 | `((close - open) / ((high - low) + .001))` |

*coverage = fraction of (date,symbol) cells that are finite; warm-up row = first
trading-day index with any finite value (large for long-window/deep-nest alphas).*