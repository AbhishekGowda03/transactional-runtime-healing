### Core cases

| setting | harm rate (bad) | unsafe-commit rate (bad) | residual-state rate (rejected) | false-rejection rate (good) | median time/case | median extra vs no repair |
|---|---|---|---|---|---|---|

### Limit cases (written to find where the guard stops working)

| setting | harm rate (bad) | unsafe-commit rate (bad) | residual-state rate (rejected) | false-rejection rate (good) | median time/case | median extra vs no repair |
|---|---|---|---|---|---|---|

### Operations the Python 3.7 wrappers do not notice (audit hooks notice them)

| setting | harm rate (bad) | unsafe-commit rate (bad) | residual-state rate (rejected) | false-rejection rate (good) | median time/case | median extra vs no repair |
|---|---|---|---|---|---|---|
| plain | 2/2 = 100% [16-100] | 0/2 = 0% [0-84] | 2/2 = 100% [16-100] | n/a | 0.532s | -11 ms |
| plain+taint | 2/2 = 100% [16-100] | 0/2 = 0% [0-84] | 2/2 = 100% [16-100] | n/a | 0.540s | -2 ms |
| fork only | 2/2 = 100% [16-100] | 0/2 = 0% [0-84] | 2/2 = 100% [16-100] | n/a | 0.529s | -14 ms |
| fork + file undo | 2/2 = 100% [16-100] | 0/2 = 0% [0-84] | 2/2 = 100% [16-100] | n/a | 0.534s | -8 ms |
| full, no file undo | 2/2 = 100% [16-100] | 0/2 = 0% [0-84] | 2/2 = 100% [16-100] | n/a | 0.517s | -25 ms |
| full protocol | 2/2 = 100% [16-100] | 0/2 = 0% [0-84] | 2/2 = 100% [16-100] | n/a | 0.539s | -3 ms |
