{
    'name': 'Perf Lab (training)',
    'summary': 'Reproduce an N+1 problem and a slow SQL query for performance training',
    'description': """
Training module for Odoo support. NOT for production databases.

* Generates test data with raw SQL (fast, repeatable)
* Scenario A: N+1 queries (search() inside a loop, slow computed field in a list view)
* Scenario B: slow SQL query (missing index + correlated subquery)
""",
    'version': '20.0.1.0.0',
    'author': 'Support training',
    'category': 'Technical',
    'license': 'LGPL-3',
    'depends': ['base'],
    'data': [
        'security/ir.access.csv',
        'views/perf_lab_views.xml',
    ],
    'application': True,
    'installable': True,
}
