SELECT DISTINCT constituents.Symbol, Action FROM constituents
LEFT JOIN account_history ON constituents.Symbol = account_history.Symbol
LEFT JOIN portfolio on constituents.Symbol = portfolio.Symbol
WHERE "Run Date" > '2021-02-23'
AND ({{get_length('n_length', 'constituents::Name')}} > 3 OR {{starts_with('A', 'portfolio::Symbol')}})
AND portfolio.Symbol IS NOT NULL
ORDER BY LENGTH(Name) LIMIT 1