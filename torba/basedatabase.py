import logging
from typing import List, Union

import sqlite3
from twisted.internet import defer
from twisted.enterprise import adbapi

import torba.baseaccount

log = logging.getLogger(__name__)


class SQLiteMixin(object):

    CREATE_TABLES_QUERY = None

    def __init__(self, path):
        self._db_path = path
        self.db = None

    def start(self):
        log.info("connecting to database: %s", self._db_path)
        self.db = adbapi.ConnectionPool(
            'sqlite3', self._db_path, cp_min=1, cp_max=1, check_same_thread=False
        )
        return self.db.runInteraction(
            lambda t: t.executescript(self.CREATE_TABLES_QUERY)
        )

    def stop(self):
        self.db.close()
        return defer.succeed(True)

    def _debug_sql(self, sql):
        """ For use during debugging to execute arbitrary SQL queries without waiting on reactor. """
        conn = self.db.connectionFactory(self.db)
        trans = self.db.transactionFactory(self, conn)
        return trans.execute(sql).fetchall()

    def _insert_sql(self, table, data):
        # type: (str, dict) -> tuple[str, List]
        columns, values = [], []
        for column, value in data.items():
            columns.append(column)
            values.append(value)
        sql = "REPLACE INTO {} ({}) VALUES ({})".format(
            table, ', '.join(columns), ', '.join(['?'] * len(values))
        )
        return sql, values

    @defer.inlineCallbacks
    def query_one_value_list(self, query, params):
        # type: (str, Union[dict,tuple]) -> defer.Deferred[List]
        result = yield self.db.runQuery(query, params)
        if result:
            defer.returnValue([i[0] for i in result])
        else:
            defer.returnValue([])

    @defer.inlineCallbacks
    def query_one_value(self, query, params=None, default=None):
        result = yield self.db.runQuery(query, params)
        if result:
            defer.returnValue(result[0][0])
        else:
            defer.returnValue(default)

    @defer.inlineCallbacks
    def query_dict_value_list(self, query, fields, params=None):
        result = yield self.db.runQuery(query.format(', '.join(fields)), params)
        if result:
            defer.returnValue([dict(zip(fields, r)) for r in result])
        else:
            defer.returnValue([])

    @defer.inlineCallbacks
    def query_dict_value(self, query, fields, params=None, default=None):
        result = yield self.query_dict_value_list(query, fields, params)
        if result:
            defer.returnValue(result[0])
        else:
            defer.returnValue(default)

    def query_count(self, sql, params):
        return self.query_one_value(
            "SELECT count(*) FROM ({})".format(sql), params
        )

    def insert_and_return_id(self, table, data):
        def do_insert(t):
            t.execute(*self._insert_sql(table, data))
            return t.lastrowid
        return self.db.runInteraction(do_insert)


class BaseDatabase(SQLiteMixin):

    CREATE_TX_TABLE = """
        create table if not exists tx (
            txid blob primary key,
            raw blob not null,
            height integer not null,
            is_confirmed boolean not null,
            is_verified boolean not null
        );
    """

    CREATE_PUBKEY_ADDRESS_TABLE = """
        create table if not exists pubkey_address (
            address blob primary key,
            account blob not null,
            chain integer not null,
            position integer not null,
            pubkey blob not null,
            history text,
            used_times integer default 0
        );
    """

    CREATE_TXO_TABLE = """
        create table if not exists txo (
            txoid integer primary key,
            txid blob references tx,
            address blob references pubkey_address,
            position integer not null,
            amount integer not null,
            script blob not null
        );
    """

    CREATE_TXI_TABLE = """
        create table if not exists txi (
            txid blob references tx,
            address blob references pubkey_address,
            txoid integer references txo
        );
    """

    CREATE_TABLES_QUERY = (
        CREATE_TX_TABLE +
        CREATE_PUBKEY_ADDRESS_TABLE +
        CREATE_TXO_TABLE +
        CREATE_TXI_TABLE
    )

    def add_transaction(self, address, hash, tx, height, is_confirmed, is_verified):
        def _steps(t):
            if not t.execute("SELECT 1 FROM tx WHERE txid=?", (sqlite3.Binary(tx.id),)).fetchone():
                t.execute(*self._insert_sql('tx', {
                    'txid': sqlite3.Binary(tx.id),
                    'raw': sqlite3.Binary(tx.raw),
                    'height': height,
                    'is_confirmed': is_confirmed,
                    'is_verified': is_verified
                }))
            for txo in tx.outputs:
                if txo.script.is_pay_pubkey_hash and txo.script.values['pubkey_hash'] == hash:
                    t.execute(*self._insert_sql("txo", {
                        'txid': sqlite3.Binary(tx.id),
                        'address': sqlite3.Binary(address),
                        'position': txo.index,
                        'amount': txo.amount,
                        'script': sqlite3.Binary(txo.script.source)
                    }))
                elif txo.script.is_pay_script_hash:
                    # TODO: implement script hash payments
                    print('Database.add_transaction pay script hash is not implemented!')

            for txi in tx.inputs:
                txoid = t.execute(
                    "SELECT txoid, address FROM txo WHERE txid = ? AND position = ?",
                    (sqlite3.Binary(txi.output_txid), txi.output_index)
                ).fetchone()
                if txoid:
                    t.execute(*self._insert_sql("txi", {
                        'txid': sqlite3.Binary(tx.id),
                        'address': sqlite3.Binary(address),
                        'txoid': txoid,
                    }))
        return self.db.runInteraction(_steps)

    @defer.inlineCallbacks
    def get_balance_for_account(self, account):
        result = yield self.db.runQuery(
            "SELECT SUM(amount) FROM txo NATURAL JOIN pubkey_address WHERE account=:account AND "
            "txoid NOT IN (SELECT txoid FROM txi)",
            {'account': sqlite3.Binary(account.public_key.address)}
        )
        if result:
            defer.returnValue(result[0][0] or 0)
        else:
            defer.returnValue(0)

    @defer.inlineCallbacks
    def get_utxos(self, account, output_class):
        utxos = yield self.db.runQuery(
            """
            SELECT amount, script, txid, position
            FROM txo NATURAL JOIN pubkey_address 
            WHERE account=:account AND txoid NOT IN (SELECT txoid FROM txi)
            """,
            {'account': sqlite3.Binary(account.public_key.address)}
        )
        defer.returnValue([
            output_class(
                values[0],
                output_class.script_class(values[1]),
                values[2],
                index=values[3]
            ) for values in utxos
        ])

    def add_keys(self, account, chain, keys):
        sql = (
            "insert into pubkey_address "
            "(address, account, chain, position, pubkey) "
            "values "
        ) + ', '.join(['(?, ?, ?, ?, ?)'] * len(keys))
        values = []
        for position, pubkey in keys:
            values.append(sqlite3.Binary(pubkey.address))
            values.append(sqlite3.Binary(account.public_key.address))
            values.append(chain)
            values.append(position)
            values.append(sqlite3.Binary(pubkey.pubkey_bytes))
        return self.db.runOperation(sql, values)

    def get_addresses(self, account, chain, limit=None, details=False):
        sql = ["SELECT {} FROM pubkey_address WHERE account = :account"]
        params = {'account': sqlite3.Binary(account.public_key.address)}
        if chain is not None:
            sql.append("AND chain = :chain")
            params['chain'] = chain
        sql.append("ORDER BY position DESC")
        if limit is not None:
            sql.append("LIMIT {}".format(limit))
        if details:
            return self.query_dict_value_list(' '.join(sql), ('address', 'position', 'used_times'), params)
        else:
            return self.query_one_value_list(' '.join(sql).format('address'), params)

    def _used_address_sql(self, account, chain, comparison_op, used_times, limit=None):
        sql = [
            "SELECT address FROM pubkey_address",
            "WHERE account = :account AND"
        ]
        params = {
            'account': sqlite3.Binary(account.public_key.address),
            'used_times': used_times
        }
        if chain is not None:
            sql.append("chain = :chain AND")
            params['chain'] = chain
        sql.append("used_times {} :used_times".format(comparison_op))
        sql.append("ORDER BY used_times ASC")
        if limit is not None:
            sql.append('LIMIT {}'.format(limit))
        return ' '.join(sql), params

    def get_unused_addresses(self, account, chain):
        # type: (torba.baseaccount.BaseAccount, int) -> defer.Deferred[List[str]]
        return self.query_one_value_list(*self._used_address_sql(
            account, chain, '=', 0
        ))

    def get_usable_addresses(self, account, chain, max_used_times, limit):
        return self.query_one_value_list(*self._used_address_sql(
            account, chain, '<=', max_used_times, limit
        ))

    def get_address(self, address):
        return self.query_dict_value(
            "SELECT {} FROM pubkey_address WHERE address= :address",
            ('address', 'account', 'chain', 'position', 'pubkey', 'history', 'used_times'),
            {'address': sqlite3.Binary(address)}
        )

    def set_address_history(self, address, history):
        return self.db.runOperation(
            "UPDATE pubkey_address SET history = ?, used_times = ? WHERE address = ?",
            (sqlite3.Binary(history), history.count(b':')//2, sqlite3.Binary(address))
        )
