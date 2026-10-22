import logging
import time

from psycopg2 import errors as pg_errors

from odoo import fields, models

_logger = logging.getLogger(__name__)

# Scenario B query: for each of the first N events, count the events sharing the
# same "ref". Without an index on perf_lab_event.ref, PostgreSQL does a full
# sequential scan of the table for EVERY outer row (quadratic behaviour).
# With the index, every inner lookup is a fast index scan.
SLOW_SQL = """
    SELECT e.id,
           (SELECT count(*) FROM perf_lab_event x WHERE x.ref = e.ref) AS same_ref
      FROM perf_lab_event e
     ORDER BY e.id
     LIMIT %s
"""
INDEX_NAME = 'perf_lab_event_ref_idx'


class PerfLabCustomer(models.Model):
    _name = 'perf.lab.customer'
    _description = 'Perf Lab - Customer'

    name = fields.Char(required=True)
    country = fields.Char()
    order_ids = fields.One2many('perf.lab.order', 'customer_id')
    total_spent_slow = fields.Float(
        string='Total spent (slow compute)',
        compute='_compute_total_spent_slow',
    )

    def _compute_total_spent_slow(self):
        """BAD PRACTICE ON PURPOSE: 2 SQL queries per record (N+1).

        Shown in the list view: 80 rows per page => ~160 queries.
        Change the pager to 1-500 to make it painful.
        """
        Order = self.env['perf.lab.order']
        for customer in self:
            orders = Order.search([('customer_id', '=', customer.id)])  # query 1
            customer.total_spent_slow = sum(orders.mapped('amount_total'))  # query 2


class PerfLabOrder(models.Model):
    _name = 'perf.lab.order'
    _description = 'Perf Lab - Order'

    name = fields.Char(required=True)
    customer_id = fields.Many2one(
        'perf.lab.customer', required=True, index=True, ondelete='cascade')
    date_order = fields.Date()
    amount_total = fields.Float()


class PerfLabEvent(models.Model):
    _name = 'perf.lab.event'
    _description = 'Perf Lab - Event'

    # intentionally NOT indexed (Scenario B)
    ref = fields.Char(index=False)
    value = fields.Integer()


class PerfLabControl(models.TransientModel):
    _name = 'perf.lab.control'
    _description = 'Perf Lab - Control panel'

    n_customers = fields.Integer(string='Customers', default=500)
    orders_per_customer = fields.Integer(string='Orders per customer', default=40)
    n_events = fields.Integer(string='Events (rows for the slow query)', default=200000)
    loop_rows = fields.Integer(
        string='Rows for the loop scenarios', default=2000,
        help='Number of orders used by scenario C (customers: at most this many).')
    slow_query_rows = fields.Integer(
        string='Outer rows for the slow query', default=1000,
        help='Higher = slower. Runtime grows linearly with this value when the '
             'index is missing.')
    timeout_s = fields.Integer(
        string='SQL timeout (seconds)', default=60,
        help='statement_timeout applied to the slow query. 0 = no limit '
             '(Odoo will then kill the worker after limit_time_real, 120s by default).')
    result = fields.Text(string='Results', readonly=True)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _log_result(self, text):
        self.ensure_one()
        self.result = ((self.result or '') + text + '\n\n').lstrip('\n')

    def _measure(self, func):
        """Run func() with a cold ORM cache; return (value, seconds, nb_queries)."""
        cr = self.env.cr
        self.env.flush_all()
        self.env.invalidate_all()  # otherwise a 2nd run benefits from the cache
        q0 = getattr(cr, 'sql_log_count', 0)
        t0 = time.perf_counter()
        value = func()
        self.env.flush_all()  # deferred writes are only sent to SQL at flush time
        elapsed = time.perf_counter() - t0
        queries = getattr(cr, 'sql_log_count', 0) - q0
        return value, elapsed, queries

    def _set_timeout(self):
        self.env.cr.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            [str(max(self.timeout_s, 0) * 1000)],
        )

    def _index_exists(self):
        self.env.cr.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s", [INDEX_NAME])
        return bool(self.env.cr.fetchone())

    # ------------------------------------------------------------------
    # Data generation (raw SQL => fast and repeatable)
    # ------------------------------------------------------------------
    def action_generate_data(self):
        self.ensure_one()
        cr = self.env.cr
        now = "(now() at time zone 'utc')"
        cr.execute(
            "TRUNCATE perf_lab_order, perf_lab_customer, perf_lab_event RESTART IDENTITY")
        cr.execute(f"""
            INSERT INTO perf_lab_customer (name, country, create_date, write_date)
            SELECT 'Customer ' || g,
                   (ARRAY['BE','FR','US','IN','ES'])[1 + (g %% 5)],
                   {now}, {now}
              FROM generate_series(1, %s) AS g
        """, [self.n_customers])
        cr.execute(f"""
            INSERT INTO perf_lab_order
                   (name, customer_id, date_order, amount_total, create_date, write_date)
            SELECT 'SO' || lpad(c.id::text, 5, '0') || '-' || g,
                   c.id,
                   current_date - (random() * 365)::int,
                   round((random() * 1000)::numeric, 2),
                   {now}, {now}
              FROM perf_lab_customer c
             CROSS JOIN generate_series(1, %s) AS g
        """, [self.orders_per_customer])
        cr.execute(f"""
            INSERT INTO perf_lab_event (ref, value, create_date, write_date)
            SELECT 'REF-' || (random() * %s)::int,
                   (random() * 1000)::int,
                   {now}, {now}
              FROM generate_series(1, %s)
        """, [max(self.n_events // 50, 1), self.n_events])
        cr.execute("ANALYZE perf_lab_customer")
        cr.execute("ANALYZE perf_lab_order")
        cr.execute("ANALYZE perf_lab_event")
        self.env.invalidate_all()
        counts = {}
        for table in ('perf_lab_customer', 'perf_lab_order', 'perf_lab_event'):
            cr.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 (constant names)
            counts[table] = cr.fetchone()[0]
        self._log_result(
            "DATA GENERATED\n"
            f"  customers : {counts['perf_lab_customer']:,}\n"
            f"  orders    : {counts['perf_lab_order']:,}\n"
            f"  events    : {counts['perf_lab_event']:,}"
        )
        return self._reopen()

    # ------------------------------------------------------------------
    # Scenario A: N+1
    # ------------------------------------------------------------------
    def action_run_n_plus_one(self):
        self.ensure_one()

        def work():
            Order = self.env['perf.lab.order']
            customers = self.env['perf.lab.customer'].search([])
            total = 0.0
            for customer in customers:
                # BAD: a search() + a read for every single customer
                orders = Order.search([('customer_id', '=', customer.id)])
                total += sum(orders.mapped('amount_total'))
            return total, len(customers)

        (total, nb), elapsed, queries = self._measure(work)
        self._log_result(
            "SCENARIO A - N+1 (search() inside a loop)\n"
            f"  customers processed : {nb:,}\n"
            f"  grand total         : {total:,.2f}\n"
            f"  elapsed             : {elapsed:.2f} s\n"
            f"  SQL queries         : {queries:,}"
        )
        return self._reopen()

    def action_run_n_plus_one_fixed(self):
        self.ensure_one()

        def work():
            customers = self.env['perf.lab.customer'].search([])
            total = 0.0
            for customer in customers:
                # GOOD: the ORM prefetches order_ids / amount_total for ALL
                # customers of the recordset in a handful of queries
                total += sum(customer.order_ids.mapped('amount_total'))
            return total, len(customers)

        (total, nb), elapsed, queries = self._measure(work)
        self._log_result(
            "SCENARIO A - FIXED (relies on ORM prefetch)\n"
            f"  customers processed : {nb:,}\n"
            f"  grand total         : {total:,.2f}\n"
            f"  elapsed             : {elapsed:.2f} s\n"
            f"  SQL queries         : {queries:,}"
        )
        return self._reopen()

    # ------------------------------------------------------------------
    # Scenario C: the typical ORM loop mistakes, each next to its fix
    # ------------------------------------------------------------------
    def action_run_loop_scenarios(self):
        self.ensure_one()
        Order = self.env['perf.lab.order']
        Customer = self.env['perf.lab.customer']
        order_ids = Order.search([], limit=self.loop_rows, order='id').ids
        customer_ids = Customer.search([], limit=self.loop_rows, order='id').ids
        today = fields.Date.today()

        def plain_loop():
            # Looks "wrong" but is FINE: one recordset => ORM prefetches
            for order in Order.browse(order_ids):
                order.customer_id.name

        def browse_in_loop():
            # BAD: a new single-record recordset per iteration => no prefetch
            for oid in order_ids:
                Order.browse(oid).customer_id.name

        def search_count_in_loop():
            # BAD: one SELECT count(*) per customer
            for cid in customer_ids:
                Order.search_count([('customer_id', '=', cid)])

        def prefetch_count():
            # GOOD: o2m prefetched for the whole recordset
            for customer in Customer.browse(customer_ids):
                len(customer.order_ids)

        def write_in_loop():
            # Odoo 18 defers writes and sends them in batches of 100 rows,
            # so this alone is not as bad as it looks
            for order in Order.browse(order_ids):
                order.write({'date_order': today})

        def write_and_search_in_loop():
            # BAD: the search on date_order forces a flush of the pending
            # write at every iteration => 1 UPDATE + 1 SELECT per record
            for order in Order.browse(order_ids):
                order.write({'date_order': today})
                Order.search_count([('date_order', '=', today)])

        def single_write():
            # GOOD: one write on the whole recordset
            Order.browse(order_ids).write({'date_order': today})

        scenarios = [
            ('OK  ', 'plain loop over ONE recordset (prefetch)', plain_loop),
            ('BAD ', 'browse(id) inside a loop', browse_in_loop),
            ('BAD ', 'search_count() inside a loop', search_count_in_loop),
            ('GOOD', 'o2m prefetch instead of search_count', prefetch_count),
            ('OK? ', 'write() in a loop (batched by ORM, v18)', write_in_loop),
            ('BAD ', 'write() + search() in the same loop', write_and_search_in_loop),
            ('GOOD', 'single write() on the recordset', single_write),
        ]
        lines = [
            "SCENARIO C - typical loop mistakes "
            f"({len(order_ids):,} orders / {len(customer_ids):,} customers)"
        ]
        for tag, label, func in scenarios:
            _value, elapsed, queries = self._measure(func)
            unit = 'query  ' if queries == 1 else 'queries'
            lines.append(f"  {tag} {label:<44} {queries:>7,} {unit} {elapsed:>7.2f} s")
        self._log_result("\n".join(lines))
        return self._reopen()

    # ------------------------------------------------------------------
    # Scenario B: slow SQL
    # ------------------------------------------------------------------
    def action_run_slow_query(self):
        self.ensure_one()
        cr = self.env.cr
        self._set_timeout()
        has_index = self._index_exists()
        t0 = time.perf_counter()
        try:
            with cr.savepoint():
                cr.execute(SLOW_SQL, [self.slow_query_rows])
                rows = cr.fetchall()
            outcome = f"finished, {len(rows):,} rows returned"
        except pg_errors.QueryCanceled:
            outcome = f"CANCELLED by statement_timeout ({self.timeout_s}s)"
        elapsed = time.perf_counter() - t0
        self._log_result(
            "SCENARIO B - slow SQL query\n"
            f"  index on event.ref  : {'YES' if has_index else 'NO'}\n"
            f"  outer rows          : {self.slow_query_rows:,}\n"
            f"  outcome             : {outcome}\n"
            f"  elapsed             : {elapsed:.2f} s"
        )
        return self._reopen()

    def action_explain_slow_query(self):
        self.ensure_one()
        cr = self.env.cr
        self._set_timeout()
        try:
            with cr.savepoint():
                cr.execute("EXPLAIN (ANALYZE, BUFFERS) " + SLOW_SQL, [self.slow_query_rows])
                plan = "\n".join(row[0] for row in cr.fetchall())
        except pg_errors.QueryCanceled:
            plan = f"(EXPLAIN ANALYZE cancelled by statement_timeout after {self.timeout_s}s)"
        self._log_result(
            f"EXPLAIN (ANALYZE, BUFFERS) - index on ref: "
            f"{'YES' if self._index_exists() else 'NO'}\n{plan}"
        )
        return self._reopen()

    def action_create_index(self):
        self.ensure_one()
        self.env.cr.execute(
            f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON perf_lab_event (ref)")
        self._log_result(f"INDEX {INDEX_NAME} CREATED (the fix for scenario B)")
        return self._reopen()

    def action_drop_index(self):
        self.ensure_one()
        self.env.cr.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
        self._log_result(f"INDEX {INDEX_NAME} DROPPED (scenario B is broken again)")
        return self._reopen()

    def action_clear_results(self):
        self.ensure_one()
        self.result = False
        return self._reopen()
