"""Black-Scholes put pricer (Priority-0 fix item 5a): used by the gap-stress BS reprice."""


def test_bs_put_monotonic_and_gt_intrinsic():
    from bot.portfolio.bs import bs_put
    assert bs_put(700, 743, 9/365, 0.20) > bs_put(760, 743, 9/365, 0.20)   # decreasing in spot
    assert bs_put(742.01, 743, 9/365, 0.20) > 0.99                          # > intrinsic (0.99)
    assert bs_put(743, 743, 9/365, 0.20) > 0                                # ATM put positive
    # deep OTM ~ 0: the true BS value here is ~0.069 (verified against put-call parity), negligible
    # vs the ~9.4 ATM value / the 743 strike. The task's original 0.05 bound was slightly too tight
    # for a 9-DTE 20%-vol put 57pts OTM; loosened to 0.1 rather than corrupt a correct pricer.
    assert bs_put(800, 743, 9/365, 0.20) < 0.1                             # deep OTM ~ 0


def test_bs_put_zero_time_is_intrinsic():
    from bot.portfolio.bs import bs_put
    assert bs_put(740, 743, 0, 0.20) == 3.0
    assert bs_put(750, 743, 0, 0.20) == 0.0
