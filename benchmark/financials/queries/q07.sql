SELECT w."Percent of Account" FROM (SELECT * FROM "portfolio" WHERE Quantity > 200 OR "Today''s Gain/Loss Percent" > 0.05) as w
JOIN {{
    do_join(
        left_on='geographic::Symbol',
        right_on='w::Symbol'
    )
}} WHERE {{starts_with('F', 'w::Symbol')}}
AND w."Percent of Account" < 0.2