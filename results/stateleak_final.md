### Core cases

| setting | harm rate (bad) | unsafe-commit rate (bad) | residual-state rate (rejected) | false-rejection rate (good) | median time/case | median extra vs no repair |
|---|---|---|---|---|---|---|
| plain | 23/23 = 100% [85-100] | 5/23 = 22% [7-44] | 18/18 = 100% [81-100] | 0/10 = 0% [0-31] | 0.038s | +0 ms |
| plain+taint | 12/23 = 52% [31-73] | 3/23 = 13% [3-34] | 12/20 = 60% [36-81] | 0/10 = 0% [0-31] | 0.040s | +2 ms |
| fork only | 7/23 = 30% [13-53] | 4/23 = 17% [5-39] | 3/19 = 16% [3-40] | 1/10 = 10% [0-45] | 0.045s | +6 ms |
| fork + file undo | 4/23 = 17% [5-39] | 4/23 = 17% [5-39] | 0/19 = 0% [0-18] | 1/10 = 10% [0-45] | 0.045s | +7 ms |
| full, no file undo | 0/23 = 0% [0-15] | 3/23 = 13% [3-34] | 0/20 = 0% [0-17] | 1/10 = 10% [0-45] | 0.045s | +7 ms |
| full protocol | 0/23 = 0% [0-15] | 3/23 = 13% [3-34] | 0/20 = 0% [0-17] | 1/10 = 10% [0-45] | 0.044s | +6 ms |

### Limit cases (written to find where the guard stops working)

| setting | harm rate (bad) | unsafe-commit rate (bad) | residual-state rate (rejected) | false-rejection rate (good) | median time/case | median extra vs no repair |
|---|---|---|---|---|---|---|
| plain | 6/6 = 100% [54-100] | 5/6 = 83% [36-100] | 1/1 = 100% [2-100] | 0/3 = 0% [0-71] | 0.040s | +3 ms |
| plain+taint | 5/6 = 83% [36-100] | 5/6 = 83% [36-100] | 1/1 = 100% [2-100] | 3/3 = 100% [29-100] | 0.041s | +3 ms |
| fork only | 6/6 = 100% [54-100] | 5/6 = 83% [36-100] | 2/2 = 100% [16-100] | 0/3 = 0% [0-71] | 0.046s | +8 ms |
| fork + file undo | 6/6 = 100% [54-100] | 5/6 = 83% [36-100] | 1/2 = 50% [1-99] | 0/3 = 0% [0-71] | 0.046s | +9 ms |
| full, no file undo | 5/6 = 83% [36-100] | 5/6 = 83% [36-100] | 2/2 = 100% [16-100] | 3/3 = 100% [29-100] | 0.047s | +10 ms |
| full protocol | 5/6 = 83% [36-100] | 5/6 = 83% [36-100] | 1/2 = 50% [1-99] | 3/3 = 100% [29-100] | 0.046s | +8 ms |
