| setting | residual-state rate (rejected attempts) | unsafe-commit rate (bad cases) | harm rate (bad cases) | false-rejection rate (good cases) | median time/case |
|---|---|---|---|---|---|
| plain | 18/18 = 100% [100-100] | 5/23 = 22% [9-39] | 23/23 = 100% [100-100] | 0/10 = 0% [0-0] | 0.03s |
| healguard-lite | 12/20 = 60% [40-80] | 3/23 = 13% [0-26] | 12/23 = 52% [30-74] | 0/10 = 0% [0-0] | 0.04s |
| protocol | 3/20 = 15% [0-30] | 4/23 = 17% [4-35] | 7/23 = 30% [13-48] | 1/10 = 10% [0-30] | 0.04s |
| protocol+guard | 0/21 = 0% [0-0] | 3/23 = 13% [0-26] | 0/23 = 0% [0-0] | 1/10 = 10% [0-30] | 0.04s |
