# Known answers

Cases with verified expected results, for unit tests of deterministic tools and as seeds for
golden evals. Numeric answers were computed and checked on 2026-11-11 (see
`scripts/data/bed_costs.py` and `scripts/data/build_planting_calendar.py`). Behavioral cases
describe the expected behavior and **need the owner's confirmation** before they become golden
answers. Add new cases here as they are verified.

## Unit conversion and price per cup (ERS)

Formula: `price_per_cup = price_per_lb / preparation_yield_factor * cup_equivalent_lb`. It matches
ERS's published `AveragePricePerCupEquivalent` within **0.001 USD**, not exactly, because the CSV
rounds its yield and cup-size columns. Tests must use that tolerance.

| ERS item (form) | Price per lb | Prep yield | Cup (lb) | Published price per cup |
| --- | --- | --- | --- | --- |
| Potatoes (Fresh) | 0.921544199 | 0.8113 | 0.2646 | 0.300503543 |
| Black beans (Dried) | 1.658510355 | 2.4692 | 0.3858 | 0.259142243 |
| Collard greens (Fresh) | 2.483338628 | 1.16 | 0.2866 | 0.613558 |
| Tomatoes, large round (Fresh) | 2.142409751 | 0.91 | 0.3748 | 0.882356978 |
| Okra (Fresh) | 5.680314126 | 0.7695 | 0.3527 | 2.603951096 |
| Sweet potatoes (Fresh) | 1.164626523 | 0.8818 | 0.4409 | 0.582313262 |

A yield factor above 1 is expected for dried beans (they absorb water when cooked) and for some
cooked greens (collard greens: 1.16).

**ERS swapped two pairs of published values** (found 2026-11-11 by the P0-4 loader test). For
Artichoke (Fresh, Canned) and Lima beans (Frozen, Dried), each row's own price, yield, and cup size
give the *other* row's published price per cup, within 0.0001. The inputs agree with each other,
so the published column is the likely error. The app computes from the inputs:

| ERS item (form) | Computed from the row | ERS published |
| --- | --- | --- |
| Lima beans (Dried), used for `lima-bean` | 0.403 | 0.968 |
| Lima beans (Frozen) | 0.968 | 0.403 |
| Artichoke (Fresh) | 2.579 | 2.080 |
| Artichoke (Canned) | 2.080 | 2.579 |

## Price per cup for any unit (normalize_price, P1-4)

| Price as written | Edible grams | Cups | Cost per cup |
| --- | --- | --- | --- |
| Onions, 3 lb bag, 2.99 | 1,224.7 | 7.6552 | 0.3906 |
| Potatoes, 5 lb bag, 3.49 | 1,840.0 | 15.3307 | 0.2276 |
| Collards, 1 lb, 2.48 | 526.2 | 4.0475 | 0.6127 |
| Green peppers, 2 large for 2.00 | 328.0 | 2.7329 | 0.7318 |
| Potatoes, 3 medium for 1.50 | 576.0 | 4.7994 | 0.3125 |
| Sweet corn, 4 medium ears for 2.00 | 612.0 | 3.7087 | 0.5393 |

## Weight of one piece (SR28, P1-3)

Purchased grams = edible grams / (1 - refuse / 100). Source: USDA SR28 `WEIGHT.txt` and
`FOOD_DES.txt`.

| Piece | Edible grams | Refuse | Purchased grams | Check |
| --- | --- | --- | --- | --- |
| Green bell pepper, large | 164 | 18% | 200.0 | SR28: "2-1/4 per lb" = 201.6 g |
| Eggplant, peeled weight | 458 | 19% | 565.4 | SR28: "yield from 1-1/4 lb" = 567.0 g |
| Sweet corn, medium ear | 102 (kernels) | 64% | 283.3 | Husk, silk, and cob |
| Tomato, medium | 123 | 9% | 135.2 | |
| Okra, 1 pod (SR28 gives 8 pods = 95 g) | 11.875 | 14% | 13.81 | Divide by the amount first |
| Potato, medium (flesh and skin) | 213 | 10% (owner; SR28 gives 25%) | 236.7 | Owner override, 2026-11-11 |

## Garden supplies: unit price and quantities

| Case | Expected |
| --- | --- |
| Topsoil per cubic foot: 2.90 per 1 cu ft; 2.99, 3.28, and 3.99 per 0.75 cu ft | 2.90; 3.99 (3.9867); 4.37 (4.3733); 5.32 |
| Cheapest topsoil per cubic foot among those four | The 1 cu ft bag at 2.90, even though the 0.75 cu ft bag at 2.99 has a similar shelf price |
| Volume of a 4 x 8 ft bed, 12 in deep | 32 cu ft = 1.185 cubic yards (a vendor FAQ claims about 0.4; the tool must not repeat that) |
| Volume at 7.5 in (one row of 1x8 cedar, actual width 7.5 in) | 20.0 cu ft |
| Volume at 15 in (two rows) | 40.0 cu ft |
| Volume at 7.25 in (one row of 2x8, actual width 7.25 in) | 19.33 cu ft |
| One 4x8 bed, one row of 1x8 cedar at 25.65 per board, 3 boards, 20 bags at 2.90 | Frame 76.95 + soil 58.00 = 134.95 |
| Same, two rows (6 boards, 40 bags) | 153.90 + 116.00 = 269.90 |
| One row of 2x8 cedar at 40.94-52.80 per board, 20 bags | 180.82 to 216.40 |
| Two rows of 2x8 (6 boards, 39 bags) | 358.74 to 429.90 |
| Bulk topsoil 62.00 per yard, 3-yard minimum, versus bags at 2.90 per cu ft | 81 cu ft costs 186.00 plus delivery versus 234.90 in bags; bulk wins only if delivery is under 48.90 |
| Seed kits per packet: 8.99 / 20; 14.99 / 40; 29.99 / 55; 39.99 / 15 | 0.4495; 0.3748; 0.5453; 2.666 |

## Planting windows (North Alabama rule)

Rule: spring windows shift 10 days later, fall windows 10 days earlier; a month with no day spans
the whole month; non-leap reference year. These test the **date logic only**; the derived dates
are unverified as agronomy and must not be golden answers for recommendations.

| Crop, season | As published | Central window | Derived North Alabama window |
| --- | --- | --- | --- |
| Bush snap beans, fall | `Aug. 5-20` | Aug 5 to Aug 20 | Jul 26 to Aug 10 |
| Bush snap beans, spring | `April` | Apr 1 to Apr 30 | Apr 11 to May 10 |
| Green onions, fall | `October - February` | Oct 1 to Feb 28 (crosses the year) | Sep 21 to Feb 18 |
| Lettuces, spring | `Jan. 15 - Feb.***` | Jan 15 to Feb 28 | Jan 25 to Mar 10 |
| Kohlrabi, fall | `Aug. 15 - Sept.` | Aug 15 to Sep 30 | Aug 5 to Sep 20 |
| Watermelons, fall | `June 15- 30` | Jun 15 to Jun 30 | Jun 5 to Jun 20 |

## Targets and rates

| Case | Expected |
| --- | --- |
| Produce target at 2,000 calories | 3 vegetable cups + 2 fruit cups per day = 35 cups per person per week |
| Two members at 2,000 calories | 70 cups per week |
| Member at 1,600 calories | 2.5 vegetable + 1.5 fruit cups per day |
| Labor rate from the Minnesota study (48 hours, 365 sq ft) | 13.15 hours per 100 sq ft per season |

## Behavioral cases (need owner confirmation)

| Input | Expected behavior |
| --- | --- |
| Typed line `onions 3 lb bag 2.99` | One price observation: item onion, quantity 3, unit `lb`, price_cents 299, captured_via `typed`, raw_text kept |
| Typed line `kale bunch 2.50` | Asks for the bunch weight, suggesting the user weigh it or estimate; an estimate is labeled as one; never silently guesses |
| Typed line `yellow onoins 3 lb 2.99` | Draft matched to onion, shown as a match for the user to confirm |
| Typed line `greens 2.48` | Asks which greens, listing the candidates (collards, turnip greens, mustard greens...) |
| Typed line `tomatoes 3 for $5` | Quantity 3, unit `each`, price_cents 500 for the lot; asks "Which size tomato: small, medium, or large?" before a cost per cup (a size is never assumed) |
| A question citing the vendor's "0.4 yards for a 4x8 bed, 12 in deep" | The answer uses the tool's 1.19 yards and does not repeat the vendor's figure as fact |
| An AMS report price shown to the user | Labeled as a typical sale price, never as the shelf price |
| "How many calories should I eat to lose weight?" | Explains that the app tracks produce acquired toward food independence, not diets, and gives no weight-loss advice |
| Any recommendation | Every number in the explanation matches a tool output in the trace |
