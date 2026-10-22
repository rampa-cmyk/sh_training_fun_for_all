# Perf Lab – Odoo performance training module

Reproduces two classic performance issues on demand. **Do not install on a real database.**

## Install

1. Copy the `perf_lab` folder into your addons path.
2. Restart Odoo, then Apps > Update Apps List, search "Perf Lab", install.
   (or: `./odoo-bin -d mydb -i perf_lab --stop-after-init`)
3. Open **Perf Lab > Control panel** and click **Generate / reset test data**
   (500 customers, 20k orders, 200k events, takes a couple of seconds).

> **Tested on Odoo 20.0 and 18.0** (installs cleanly, all scenarios run). This package targets **20.0**.
>
> Running another version? Two small changes:
> 1. `__manifest__.py`: change `'version'` to match your series (e.g. `18.0.1.0.0`). Odoo 20 refuses a mismatched version.
> 2. Odoo 20 replaced `ir.model.access.csv` with `ir.access.csv`. On 18 (and probably 19, untested), delete
>    `security/ir.access.csv`, point the manifest to `security/ir.model.access.csv` and use:
>
>        id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
>        access_perf_lab_customer,perf.lab.customer,model_perf_lab_customer,base.group_user,1,1,1,1
>        access_perf_lab_order,perf.lab.order,model_perf_lab_order,base.group_user,1,1,1,1
>        access_perf_lab_event,perf.lab.event,model_perf_lab_event,base.group_user,1,1,1,1
>        access_perf_lab_control,perf.lab.control,model_perf_lab_control,base.group_user,1,1,1,1
>
> For 17 or older also replace `<list>` with `<tree>` and `list,form` with `tree,form` in the views.
> The code uses `env.flush_all()` / `env.invalidate_all()` (Odoo 16+). ORM behaviour around deferred
> writes can differ between versions, so scenario C numbers may differ: a good talking point in itself.

## Before the session: make the problems observable

Start Odoo with SQL logging, so every query shows up in the log:

    ./odoo-bin -d mydb --log-handler=odoo.sql_db:DEBUG

Other tools worth showing:

* **Odoo profiler**: developer mode > bug icon > start profiling with "SQL" enabled, reproduce, stop,
  then open the result (Settings > Technical > Profiling). The SQL view groups identical queries,
  which makes an N+1 obvious.
* **pg_stat_activity** (from psql in a second terminal, while the slow query runs):

      SELECT pid, now() - query_start AS runtime, state, left(query, 120)
        FROM pg_stat_activity
       WHERE datname = current_database() AND state <> 'idle'
       ORDER BY runtime DESC;

* **PostgreSQL slow query log**: `log_min_duration_statement = 500` (ms) in postgresql.conf.

## Scenario A – N+1

| Step | What to do | What to observe |
|------|-----------|-----------------|
| 1 | Control panel > **Run N+1 (slow)** | ~1,000 queries (2 per customer) |
| 2 | **Run fixed version** | A handful of queries (4 to 22 depending on version), same total |
| 3 | Perf Lab > **Customers (slow list)**, then set the pager to `1-500` | The list view is slow; the log shows `SELECT id FROM perf_lab_order WHERE customer_id = X` repeated with a different X every time |

Root cause: `search()` inside a loop (and a computed field doing it per record) bypasses the ORM's prefetching.
Discussion points: `browse(id)` inside a loop has the same effect, `read_group`/`_read_group`
for aggregates, stored computed fields, `mapped()` on the whole recordset.

## Scenario C – Typical loop mistakes, bad vs good

Control panel > **Run all loop scenarios** runs every pattern on the same data with a cold cache and prints
queries and time side by side. Reference run on Odoo 18 and 20 (2,000 orders / 500 customers; both gave the same counts):

| Pattern | Queries | Lesson |
|---------|--------:|--------|
| OK: plain loop over ONE recordset, reading `order.customer_id.name` | 3 | Looks like a loop, but prefetch makes it cheap. Not every loop is a problem. |
| BAD: `Order.browse(id)` inside a loop | ~2,500 | A new single-record recordset has no prefetch. Same for `read()` per id. |
| BAD: `search_count()` inside a loop | 500 | One query per iteration. |
| GOOD: `len(customer.order_ids)` on a recordset | 1 | Prefetched for the whole recordset. |
| OK?: `write()` per record in a loop | 20 | In Odoo 18 and 20 writes are deferred and flushed in batches of 100 rows. |
| BAD: `write()` + `search()` in the same loop | ~4,000 (and ~5 s) | The search on a dirty field forces a flush every iteration: 1 UPDATE + 1 SELECT per record. |
| GOOD: one `write()` on the whole recordset | 20 | Same result, same batching, no flush storm. |

Discussion points: how to spot each one in the SQL log or profiler (repeated queries that differ only by id),
why "write in a loop" is not always the culprit and what really makes it slow (anything that forces a flush
or recompute inside the loop: searches, reading a stored computed field, overridden `write()`, mail tracking).

## Scenario B – Slow SQL (missing index)

| Step | What to do | What to observe |
|------|-----------|-----------------|
| 1 | **Run slow query** (default 1000 outer rows, 60s timeout) | Roughly 10 s on a typical machine (about 11 ms per outer row); watch it in `pg_stat_activity` |
| 2 | **EXPLAIN ANALYZE** | `Seq Scan on perf_lab_event x` inside a `SubPlan` with `loops=1000` |
| 3 | **Create index (fix)**, run again | Milliseconds; the plan now shows a `Bitmap Index Scan on perf_lab_event_ref_idx` and runs in ~10 ms |
| 4 | **Drop index (break)** | Back to the slow behaviour, repeat as needed |

Tuning: raise "Outer rows" to make it slower (runtime grows linearly), raise the event count and
regenerate to make each scan more expensive. Set the timeout to `0` to show what a customer sees:
the request runs until Odoo's `limit_time_real` kills the worker.

## Ideas to extend

* Add a `create()` in a loop vs. `create([vals, ...])`, ideally on a model with stored computed fields.
* Add a non-stored computed field with `search_count` and put it in a search/group-by.
* Add a `LIKE '%foo%'` search on a large text column (needs a trigram index to fix).
