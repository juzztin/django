"""
Query subclasses which provide extra functionality beyond simple data retrieval.
"""

from django.core.exceptions import FieldError
from django.db import connections
from django.db.models.sql.constants import *
from django.db.models.sql.datastructures import Date
from django.db.models.sql.expressions import SQLEvaluator
from django.db.models.sql.query import Query
from django.db.models.sql.where import AND, Constraint

__all__ = ['DeleteQuery', 'UpdateQuery', 'InsertQuery', 'DateQuery',
        'AggregateQuery']

class DeleteQuery(Query):
    """
    Delete queries are done through this class, since they are more constrained
    than general queries.
    """

    compiler = 'SQLDeleteCompiler'

    def do_query(self, table, where, using):
        self.tables = [table]
        self.where = where
        self.get_compiler(using).execute_sql(None)

    def delete_batch_related(self, pk_list, using):
        """
        Set up and execute delete queries for all the objects related to the
        primary key values in pk_list. To delete the objects themselves, use
        the delete_batch() method.

        More than one physical query may be executed if there are a
        lot of values in pk_list.
        """
        from django.contrib.contenttypes import generic
        cls = self.model
        for related in cls._meta.get_all_related_many_to_many_objects():
            if not isinstance(related.field, generic.GenericRelation):
                for offset in range(0, len(pk_list), GET_ITERATOR_CHUNK_SIZE):
                    where = self.where_class()
                    where.add((Constraint(None,
                            related.field.m2m_reverse_name(), related.field),
                            'in',
                            pk_list[offset : offset+GET_ITERATOR_CHUNK_SIZE]),
                            AND)
                    self.do_query(related.field.m2m_db_table(), where, using=using)

        for f in cls._meta.many_to_many:
            w1 = self.where_class()
            if isinstance(f, generic.GenericRelation):
                from django.contrib.contenttypes.models import ContentType
                field = f.rel.to._meta.get_field(f.content_type_field_name)
                w1.add((Constraint(None, field.column, field), 'exact',
                        ContentType.objects.get_for_model(cls).id), AND)
            for offset in range(0, len(pk_list), GET_ITERATOR_CHUNK_SIZE):
                where = self.where_class()
                where.add((Constraint(None, f.m2m_column_name(), f), 'in',
                        pk_list[offset : offset + GET_ITERATOR_CHUNK_SIZE]),
                        AND)
                if w1:
                    where.add(w1, AND)
                self.do_query(f.m2m_db_table(), where, using=using)

    def delete_batch(self, pk_list, using):
        """
        Set up and execute delete queries for all the objects in pk_list. This
        should be called after delete_batch_related(), if necessary.

        More than one physical query may be executed if there are a
        lot of values in pk_list.
        """
        for offset in range(0, len(pk_list), GET_ITERATOR_CHUNK_SIZE):
            where = self.where_class()
            field = self.model._meta.pk
            where.add((Constraint(None, field.column, field), 'in',
                    pk_list[offset : offset + GET_ITERATOR_CHUNK_SIZE]), AND)
            self.do_query(self.model._meta.db_table, where, using=using)

class UpdateQuery(Query):
    """
    Represents an "update" SQL query.
    """

    compiler = 'SQLUpdateCompiler'

    def __init__(self, *args, **kwargs):
        super(UpdateQuery, self).__init__(*args, **kwargs)
        self._setup_query()

    def _setup_query(self):
        """
        Runs on initialization and after cloning. Any attributes that would
        normally be set in __init__ should go in here, instead, so that they
        are also set up after a clone() call.
        """
        self.values = []
        self.related_ids = None
        if not hasattr(self, 'related_updates'):
            self.related_updates = {}

    def clone(self, klass=None, **kwargs):
        return super(UpdateQuery, self).clone(klass,
                related_updates=self.related_updates.copy(), **kwargs)


    def clear_related(self, related_field, pk_list, using):
        """
        Set up and execute an update query that clears related entries for the
        keys in pk_list.

        This is used by the QuerySet.delete_objects() method.
        """
        for offset in range(0, len(pk_list), GET_ITERATOR_CHUNK_SIZE):
            self.where = self.where_class()
            f = self.model._meta.pk
            self.where.add((Constraint(None, f.column, f), 'in',
                    pk_list[offset : offset + GET_ITERATOR_CHUNK_SIZE]),
                    AND)
            self.values = [(related_field, None, None)]
            self.get_compiler(using).execute_sql(None)

    def add_update_values(self, values):
        """
        Convert a dictionary of field name to value mappings into an update
        query. This is the entry point for the public update() method on
        querysets.
        """
        values_seq = []
        for name, val in values.iteritems():
            field, model, direct, m2m = self.model._meta.get_field_by_name(name)
            if not direct or m2m:
                raise FieldError('Cannot update model field %r (only non-relations and foreign keys permitted).' % field)
            if model:
                self.add_related_update(model, field, val)
                continue
            values_seq.append((field, model, val))
        return self.add_update_fields(values_seq)

    def add_update_fields(self, values_seq):
        """
        Turn a sequence of (field, model, value) triples into an update query.
        Used by add_update_values() as well as the "fast" update path when
        saving models.
        """
        self.values.extend(values_seq)

    def add_related_update(self, model, field, value):
        """
        Adds (name, value) to an update query for an ancestor model.

        Updates are coalesced so that we only run one update query per ancestor.
        """
        try:
            self.related_updates[model].append((field, None, value))
        except KeyError:
            self.related_updates[model] = [(field, None, value)]

    def get_related_updates(self):
        """
        Returns a list of query objects: one for each update required to an
        ancestor model. Each query will have the same filtering conditions as
        the current query but will only update a single table.
        """
        if not self.related_updates:
            return []
        result = []
        for model, values in self.related_updates.iteritems():
            query = UpdateQuery(model)
            query.values = values
            if self.related_ids:
                query.add_filter(('pk__in', self.related_ids))
            result.append(query)
        return result

class InsertQuery(Query):
    compiler = 'SQLInsertCompiler'

    def __init__(self, *args, **kwargs):
        super(InsertQuery, self).__init__(*args, **kwargs)
        self.columns = []
        self.values = []
        self.params = ()
        self.return_id = False

    def clone(self, klass=None, **kwargs):
        extras = {'columns': self.columns[:], 'values': self.values[:],
                  'params': self.params, 'return_id': self.return_id}
        extras.update(kwargs)
        return super(InsertQuery, self).clone(klass, **extras)

    def insert_values(self, insert_values, connection, raw_values=False):
        """
        Set up the insert query from the 'insert_values' dictionary. The
        dictionary gives the model field names and their target values.

        If 'raw_values' is True, the values in the 'insert_values' dictionary
        are inserted directly into the query, rather than passed as SQL
        parameters. This provides a way to insert NULL and DEFAULT keywords
        into the query, for example.
        """
        placeholders, values = [], []
        for field, val in insert_values:
            if hasattr(field, 'get_placeholder'):
                # Some fields (e.g. geo fields) need special munging before
                # they can be inserted.
                placeholders.append(field.get_placeholder(val, connection))
            else:
                placeholders.append('%s')

            self.columns.append(field.column)
            values.append(val)
        if raw_values:
            self.values.extend(values)
        else:
            self.params += tuple(values)
            self.values.extend(placeholders)

class DateQuery(Query):
    """
    A DateQuery is a normal query, except that it specifically selects a single
    date field. This requires some special handling when converting the results
    back to Python objects, so we put it in a separate class.
    """

    compiler = 'SQLDateCompiler'

    def add_date_select(self, field, lookup_type, order='ASC'):
        """
        Converts the query into a date extraction query.
        """
        result = self.setup_joins([field.name], self.get_meta(),
                self.get_initial_alias(), False)
        alias = result[3][-1]
        select = Date((alias, field.column), lookup_type)
        self.select = [select]
        self.select_fields = [None]
        self.select_related = False # See #7097.
        self.set_extra_mask([])
        self.distinct = True
        self.order_by = order == 'ASC' and [1] or [-1]

class AggregateQuery(Query):
    """
    An AggregateQuery takes another query as a parameter to the FROM
    clause and only selects the elements in the provided list.
    """

    compiler = 'SQLAggregateCompiler'

    def add_subquery(self, query, using):
        self.subquery, self.sub_params = query.get_compiler(using).as_sql(with_col_aliases=True)
