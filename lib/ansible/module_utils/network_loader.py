#
# Copyright (c) 2014-2017 Cisco and/or its affiliates.
# Copyright (c) 2017 Red Hat, Inc.
#
# This code is part of Ansible, but is an independent component.
#
# This particular file snippet, and this file snippet only, is BSD licensed.
# Modules you write using this snippet, which is embedded dynamically by Ansible
# still belong to the author of the module, and may assign their own license
# to the complete work.
#
# (c) 2017 Red Hat Inc.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#    * Redistributions in binary form must reproduce the above copyright notice,
#      this list of conditions and the following disclaimer in the documentation
#      and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE
# USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
import os
import re
import ast
import copy
import operator
import collections

import q

try:
    from jinja2 import Environment
    from jinja2.exceptions import UndefinedError
    HAS_JINJA2 = True
except ImportError:
    HAS_JINJA2 = False

from ansible.module_utils.network_common import dict_diff, dict_combine
from ansible.module_utils.network_common import to_list
from ansible.module_utils.six import iteritems



ATTR_PROPERTIES = frozenset(
    (
        'kind',
        'required',

        # get attributes
        'command',
        'format',
        'items',
        'multiple',
        'default_value',

        # set attributes
        'context',
        'order',

        'get_value',
        'set_value',
        'del_value',

        'getter',
        'setter',
        'deleter'
    )
)

ATTR_DEFAULTS = {
    'kind': 'str',
    'format': 'text',
    'required': False
}


_Attr = collections.namedtuple('Attr', ATTR_PROPERTIES)

def ternary(value, true_val, false_val):
    '''  value ? true_val : false_val '''
    if value:
        return true_val
    else:
        return false_val


class Loader(object):

    def __init__(self, module, connection, spec, platform=None):

        if not HAS_JINJA2:
            module.fail_json(
                msg='jinja2 is required but does not appear to be installed. '
                    'It can be installed using `pip install jinja2`'
            )

        self.module = module
        self.connection = connection
        self.platform = platform
        self.attributes = {}
        self.commands = set()
        self.env = Environment()
        self.env.filters['ternary'] = ternary

        self._load_spec(spec)

    def dump(self):
        obj = {}
        for name, attr in iteritems(self.attributes):
            obj[name ] = {}
            for key in ATTR_PROPERTIES:
                obj[name][key] = getattr(attr, key)
        return obj

    def _make_attr(self, data):
        assert isinstance(data, dict)
        kwargs = {}
        for item in ATTR_PROPERTIES:
            value = data.get(item)
            if isinstance(value, dict):
                val = {}
                for name, attrs in iteritems(value):
                    val[name] = self._make_attr(attrs)
                kwargs[item] = val
            else:
                kwargs[item] = value
            kwargs[item] = kwargs[item] or ATTR_DEFAULTS.get(item)
        return _Attr(**kwargs)

    def _load_spec(self, spec, defaults=None):
        assert isinstance(spec, dict)

        if '_defaults' in spec and not defaults:
            defaults = self._platform_spec(spec['_defaults'])
            if 'default_format' in defaults:
                ATTR_DEFAULTS['format'] = defaults['default_format']


        for key, values in iteritems(spec):
            if not key.startswith('_'):
                data = self._platform_spec(values)

                if defaults:
                    data = dict_combine(defaults, data)

                attr = self._make_attr(data)

                self.attributes[key] = attr
                self.commands.add((attr.command, attr.format))

    def _platform_spec(self, entry):
        assert isinstance(entry, dict)
        platform = entry.get(self.platform, {})
        return dict_combine(entry, platform)

    def template(self, tmpl, kind='str', variables={}):
        value = self.env.from_string(tmpl).render(variables)

        try:
            if value and kind != 'str':
                return ast.literal_eval(value)
            elif value is not None:
                return str(value)
            else:
                return None
        except:
            # XXX should this fail?
            return None

    def _get_setter(self, attr, operation='merge'):
        if operation == 'delete':
            if attr.del_value:
                return attr.del_value
            else:
                return 'no %s' % attr.set_value
        else:
            return attr.set_value

    def _populate_dict(self, attr, variables):
        assert isinstance(variables, dict), 'variables is wrong type, expected dict, got %s' % type(variables)

        obj = {}

        for name, attr in iteritems(attr.get_value):
            try:
                value = self.template(attr.get_value, (attr.kind or 'str'), variables)
            except UndefinedError:
                value = None
            obj[name] = value

        return obj

    def send_to_device(self, obj=None, operation='merge'):
        assert operation in ('delete', 'merge'), 'invalid operation specified'

        commands = list()
        current = None

        if operation == 'delete' and not obj:
            obj = self.load_from_device()

        assert isinstance(obj, dict)

        for key, value in iteritems(obj):
            variables = copy.deepcopy(obj)

            try:
                attr = self.attributes[key]
            except KeyError:
                raise
                self.module.fail_json(msg='invalid attr specified: %s' % key)

            if not attr.set_value:
                continue

            for item in to_list(value):
                if isinstance(item, tuple):
                    item, op = item
                    if operation != op:
                        operation = op
                    variables['item'] = item
                    setter = self._get_setter(attr, op)
                else:
                    variables['item'] = item
                    setter = self._get_setter(attr, operation)

                if isinstance(item, dict):
                    variables.update(item)

                # attr is readonly
                if not setter:
                    continue

                context = list()

                # set a config context before entering commands
                if operation != 'delete' and attr.context:
                    for entry in to_list(attr.context):
                        context.append(self.template(entry, variables=variables))

                if isinstance(setter, dict):
                    updates = to_list(context)

                    for item_key, item_value in iteritems(setter):
                        if item_key not in item:
                            continue

                        string = item_value.set_value
                        # attr is readonly
                        if not string:
                            continue

                        config_string = self.template(string, variables=variables)

                        if item_value.order is not None:
                            updates.insert(item_value.order, config_string)
                        else:
                            updates.append(config_string)

                    if attr.order is None:
                        commands.extend(updates)
                    else:
                        for c in updates.reverse():
                            commands.insert(attr.order, c)

                else:
                    config_string = self.template(setter, variables=variables)
                    context.append(config_string)

                    if attr.order is None:
                        commands.extend(context)
                    else:
                        for c in context.reverse():
                            commands.insert(attr.order, c)

        return commands

    def load_from_device(self):
        # run the commands on the connection object and populate the responses
        # in the responses dict
        responses = dict()
        for item, fmt in self.commands:
            # FIXME once wired up to the network_cli connection plugin
            #response = connection.get(item)
            #responses[item] = self.module.from_json(response)
            #
            rc, out, err = self.connection.exec_command(item)
            if fmt == 'json':
                import json
                responses[item] = json.loads(out)
            else:
                responses[item] = str(out).strip()

        obj = {}

        for name, attr in iteritems(self.attributes):
            output = responses[attr.command]
            variables = {'output': output}

            if attr.items:
                if attr.format == 'json':
                    items = self.template(attr.items, 'list', variables=variables)
                    variables['items'] = to_list(items)
                elif attr.multiple:
                    match = re.findall(attr.items, output, re.M)
                    variables['items'] = match
                else:
                    match = re.search(attr.items, output, re.M)
                    if match:
                        variables['items'] = list(match.groups())
                        variables.update(match.groupdict())

            if attr.kind == 'list' and isinstance(attr.get_value, dict):
                if attr.format != 'json':
                    # FIXME: this will cause re.findall() to be run twice in some
                    # cases due to above if attr.items ...
                    match = re.findall(attr.items, output, re.M)
                    variables['items'] = match

                values = list()
                for item in variables['items']:
                    item_vars = {'item': item}

                    if attr.format != 'json':
                        # in case named groups where defined in the items()
                        # regexp, this will rebind the key/value pairs
                        regex = re.compile(attr.items)
                        if regex.groupindex:
                            for key_name, index in iteritems(regex.groupindex):
                                item_vars[key_name] = item[index - 1]

                    values.append(self._populate_dict(attr, item_vars))

                value = values

            else:
                try:
                    value = self.template(attr.get_value, attr.kind, variables)
                except UndefinedError:
                    value = None

            obj[name] = value or attr.default_value

        return obj

    def load_from_params(self):
        obj = {}
        for key, value in iteritems(self.attributes):
            if key in self.module.params:
                value = self.module.params[key]
                if value:
                    obj[key] = self.module.params[key]
        return obj


class ConfigLoader(Loader):

    def send_to_device(self, obj=None, operation='merge', purge=True):
        assert operation in ('delete', 'merge'), 'invalid operation specified'

        current = self.load_from_device()
        if operation == 'delete' and not obj:
            obj = current

        assert isinstance(obj, dict), 'argument must be of type <dict>'

        diff = dict_diff(current, obj)
        loadable = {}

        for key, value in iteritems(diff):

            if isinstance(value, list):
                attr = self.attributes[key]

                haves = current.get(key, [])
                wants = obj.get(key, [])

                values = list()

                if wants:
                    if purge:
                        for item in haves:
                            if isinstance(item, dict):
                                if not self._match_dict_item(item, wants, attr):
                                    values.append((item, 'delete'))
                            elif item not in wants:
                                values.append((item, 'delete'))

                    for item in wants:
                        if isinstance(item, dict):
                            if not self._match_dict_item(item, haves, attr, exact_match=True):
                                values.append((item, 'merge'))
                        elif item not in haves:
                            values.append((item, 'merge'))

                loadable[key] = values

            else:
                loadable[key] = value

        return super(ConfigLoader, self).send_to_device(loadable, operation)

    def _match_dict_item(self, item, collection, attr, exact_match=False):
        keyed_obj = {}

        for key, value in iteritems(item):
            if attr.get_value[key].required:
                keyed_obj[key] = value

        for entry in collection:
            if set(keyed_obj.viewitems()).issubset(entry.viewitems()):
                if exact_match:
                    for k, v in iteritems(item):
                        if entry[k] != v:
                            return False
                return True

