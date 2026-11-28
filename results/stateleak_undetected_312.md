### Core cases

| setting | harm rate (bad) | unsafe-commit rate (bad) | residual-state rate (rejected) | false-rejection rate (good) | median time/case | median extra vs no repair |
|---|---|---|---|---|---|---|

### Limit cases (written to find where the guard stops working)

| setting | harm rate (bad) | unsafe-commit rate (bad) | residual-state rate (rejected) | false-rejection rate (good) | median time/case | median extra vs no repair |
|---|---|---|---|---|---|---|

### Operations the Python 3.7 wrappers do not notice (audit hooks notice them)

| setting | harm rate (bad) | unsafe-commit rate (bad) | residual-state rate (rejected) | false-rejection rate (good) | median time/case | median extra vs no repair |
|---|---|---|---|---|---|---|
| plain | 2/2 = 100% [16-100] | 0/2 = 0% [0-84] | 2/2 = 100% [16-100] | n/a | 0.051s | -1 ms |
| plain+taint | 0/2 = 0% [0-84] | 0/2 = 0% [0-84] | 0/2 = 0% [0-84] | n/a | 0.046s | -6 ms |
| fork only | 1/2 = 50% [1-99] | 0/2 = 0% [0-84] | 1/2 = 50% [1-99] | n/a | 0.049s | -2 ms |
| fork + file undo | 0/2 = 0% [0-84] | 0/2 = 0% [0-84] | 0/2 = 0% [0-84] | n/a | 0.050s | -2 ms |
| full, no file undo | 0/2 = 0% [0-84] | 0/2 = 0% [0-84] | 0/2 = 0% [0-84] | n/a | 0.050s | -2 ms |
| full protocol | 0/2 = 0% [0-84] | 0/2 = 0% [0-84] | 0/2 = 0% [0-84] | n/a | 0.049s | -3 ms |
