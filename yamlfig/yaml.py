import yaml
import re
import copy
import pickle
import tokenize
import collections

from .nodes.node import ConfigNode
from .nodes.composed import ComposedNode
from .utils import pad_with_none


_fstr_regex = re.compile(r"^\s*f(['\"]).*\1\s*$")


def _encode_metadata(metadata):
    return pickle.dumps(metadata).hex()


def _decode_metadata(encoded):
    return pickle.loads(bytes.fromhex(encoded))


def _maybe_parse_scalar(loader, node, reparse=True):
    ret = loader.construct_scalar(node)
    if reparse and isinstance(ret, str) and not ret.strip().startswith('!'):
        ret = yaml.safe_load(ret)

    return ret


def _make_obj(loader, node, objtype, reparse_scalars=False):
    args = []
    kwargs = {}
    if isinstance(node, yaml.MappingNode):
        kwargs = loader.construct_mapping(node, deep=True)
    elif isinstance(node, yaml.SequenceNode):
        args = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.ScalarNode):
        val = _maybe_parse_scalar(loader, node, reparse=reparse_scalars)
        if val:
            args = [val]

    if '*' in kwargs:
        args.extend(kwargs['*'])
        del kwargs['*']

    return objtype(*args, **kwargs)


def _make_node(loader, node, node_type=ConfigNode, kwargs=None, data_arg_name=None, dict_is_data=True):
    ''' A generic function to create new config nodes.

        Arguments:
            loader : a yaml.Loader
            node : a yaml node to parse, which will be used to create a config node
            node_type : a callable which will be called to create a config node - the following arguments control
                what are the arguments to the callable
            kwargs : a fixed directory of extra keyword arguments which will be passed to ``node_type``
            data_arg_name : if parsed node data should be passed as a keyword argument, the name of the
                argument should be specified by this argument, if it is ``None``, parsed node data
                will be passed as the first (positional) argument::

                    data = parse(node)
                    if data_arg_name:
                        kwargs[data_arg_name] = data
                        return node_type(**kwargs)
                    else:
                        return node_type(data, **kwargs)

            dict_is_data : if ``True`` and the parsed node data is mapping, the data will be used as
                a single ``"data"`` argument (see ``data_arg_name``), otherwise if ``False`` and the parsed
                node data is mapping, the dict will be treated as ``**kwargs`` for ``node_type``::

                    data = parse(node)
                    if isinstance(data, dict) and not dict_is_data:
                        kwargs.update(data)
                        return node_type(**kwargs)
                    else:
                        return node_type(data, **kwargs)

    '''
    kwargs = kwargs or {}
    data = None
    is_dict = False
    if isinstance(node, yaml.MappingNode):
        data = loader.construct_mapping(node, deep=True)
        is_dict = True
    elif isinstance(node, yaml.SequenceNode):
        data = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.ScalarNode):
        data = _maybe_parse_scalar(loader, node, reparse=node_type is ConfigNode)

    kwargs.setdefault('source_file', loader.context.get_current_file())

    if is_dict and not dict_is_data:
        kwargs.update(data)
        return node_type(**kwargs)

    if data_arg_name is None:
        return node_type(data, idx=loader.context.get_next_stage_idx(), **kwargs)
    else:
        assert data_arg_name not in kwargs
        kwargs[data_arg_name] = data
        return node_type(idx=loader.context.get_next_stage_idx(), **kwargs)


def _del_constructor(loader, node):
    return _make_node(loader, node, kwargs={ 'delete': True })


def _weak_constructor(loader, node):
    return _make_node(loader, node, kwargs={ 'merge_mode': ConfigNode.WEAK })


def _force_constructor(loader, node):
    return _make_node(loader, node, kwargs={ 'merge_mode': ConfigNode.FORCE })


def _merge_constructor(loader, node):
    return _make_node(loader, node, kwargs={ 'delete': False })


def _append_constructor(loader, node):
    from .nodes.append import AppendNode
    return _make_node(loader, node, node_type=AppendNode)


def _metadata_constructor(loader, tag_suffix, node):
    metadata = _decode_metadata(tag_suffix)
    kwargs = {}
    for special in ConfigNode.special_metadata_names:
        if special in metadata:
            kwargs[special] = metadata.pop(special)

    kwargs['metadata'] = metadata
    return _make_node(loader, node, kwargs=kwargs)


def _include_constructor(loader, node):
    from .nodes.include import IncludeNode
    return _make_node(loader, node, node_type=IncludeNode, kwargs={ 'ref_file': loader.context.get_current_file() }, dict_is_data=False)


def _prev_node_constructor(loader, node):
    from .nodes.prev import PrevNode
    return _make_node(loader, node, node_type=PrevNode)


def _xref_node_constructor(loader, node):
    from .nodes.xref import XRefNode
    return _make_node(loader, node, node_type=XRefNode)


def _simple_bind_node_constructor(loader, node):
    from .nodes.bind import BindNode
    return _make_node(loader, node, node_type=BindNode, data_arg_name='func')


def _bind_node_constructor(loader, tag_suffix, node):
    from .nodes.bind import BindNode
    if tag_suffix.count(':') > 1:
        raise ValueError(f'Invalid bind tag: !bind:{tag_suffix}')

    target_f_name, metadata = pad_with_none(*tag_suffix.split(':', maxsplit=1), minlen=2)
    return _make_node(loader, node, node_type=BindNode, kwargs={ 'func': target_f_name, 'metadata': metadata }, data_arg_name='args')


def _simple_call_node_constructor(loader, node):
    from .nodes.call import CallNode
    return _make_node(loader, node, node_type=CallNode, data_arg_name='func')


def _call_node_constructor(loader, tag_suffix, node):
    from .nodes.call import CallNode
    if tag_suffix.count(':') > 1:
        raise ValueError(f'Invalid call tag: !call:{tag_suffix}')

    target_f_name, metadata = pad_with_none(*tag_suffix.split(':', maxsplit=1), minlen=2)
    return _make_node(loader, node, node_type=CallNode, kwargs={ 'func': target_f_name, 'metadata': metadata }, data_arg_name='args')


def _eval_node_constructor(loader, node):
    from .nodes.eval import EvalNode
    return _make_node(loader, node, node_type=EvalNode)


def _fstr_node_constructor(loader, node):
    from .nodes.fstr import FStrNode

    def _maybe_fix_fstr(value, *args, **kwargs):
        try:
            return FStrNode(value, *args, **kwargs)
        except ValueError:
            return FStrNode("f'" + value.replace(r"'", r"\'") + "'", *args, **kwargs)

    return _make_node(loader, node, node_type=_maybe_fix_fstr)


def _import_node_constructor(loader, node):
    import importlib
    module = importlib.import_module('.nodes.import', package='yamlfig') # dirty hack because "import" is a keyword
    ImportNode = module.ImportNode
    return _make_node(loader, node, node_type=ImportNode)


def _required_node_constructor(loader, node):
    from .nodes.required import RequiredNode
    def _check_empty_str(arg, **kwargs):
        if arg != '':
            raise ValueError(f'!required node does not expect any arguments - got: {arg}')
        return RequiredNode(**kwargs)
    return _make_node(loader, node, node_type=_check_empty_str)


def _none_constructor(loader, node):
    def _check_empty_str(arg, *args, **kwargs):
        if arg != '' or args:
            raise ValueError(f'!null does not expect any arguments - got: {[arg]+list(args)}')
        return None
    return _make_node(loader, node, node_type=_check_empty_str)


def make_call_node_with_fixed_func(loader, node, func):
    from .nodes.call import CallNode
    return _make_node(loader, node, node_type=CallNode, kwargs={ 'func': func }, data_arg_name='args')


yaml.add_constructor('!del', _del_constructor)
yaml.add_constructor('!weak', _weak_constructor)
yaml.add_constructor('!force', _force_constructor)
yaml.add_constructor('!merge', _merge_constructor)
yaml.add_constructor('!append', _append_constructor)
yaml.add_multi_constructor('!metadata:', _metadata_constructor)
yaml.add_constructor('!include', _include_constructor)
yaml.add_constructor('!prev', _prev_node_constructor)
yaml.add_constructor('!xref', _xref_node_constructor)
yaml.add_multi_constructor('!bind:', _bind_node_constructor) # full bind form: !bind:func_name[:metadata] args_dict
yaml.add_constructor('!bind', _simple_bind_node_constructor) # simple argumentless bind from string: !bind func_name
yaml.add_multi_constructor('!call:', _call_node_constructor) # full call form: !call:func_name[:metadata] args_dict
yaml.add_constructor('!call', _simple_call_node_constructor) # simple argumentless call from string: !call func_name
yaml.add_constructor('!eval', _eval_node_constructor)
yaml.add_constructor('!fstr', _fstr_node_constructor)
yaml.add_implicit_resolver('!fstr', _fstr_regex)
yaml.add_constructor('!import', _import_node_constructor)
yaml.add_constructor('!required', _required_node_constructor)
yaml.add_constructor('!null', _none_constructor)


def _node_representer(dumper, node):
    from .nodes.bind import BindNode
    tag, metadata, data = node.yamlfigns.represent()
    if data is None:
        assert not tag
        tag = '!null'

    parent_metadata = dumper.metadata[-1] if dumper.metadata else {}
    type_defaults = node.yamlfigns.get_default_mode()

    to_infer = ['merge_mode', 'delete']
    for f in to_infer:
        if f not in metadata:
            continue

        current = metadata[f]
        parent = parent_metadata.get(f, None) if parent_metadata else None
        default = type_defaults[f]
        if current is not None:
            if current == parent or current == default:
                del metadata[f]
        else:
            del metadata[f]

    metadata = { key: value for key, value in metadata.items() if key not in dumper.exclude_metadata }

    if metadata:
        if tag is None:
            tag = '!metadata'
        tag += ':' + _encode_metadata(metadata)

    pop = False
    if isinstance(node, ComposedNode):
        dumper.metadata.append({ **parent_metadata, **metadata })
        pop = True

    try:
        if not tag and not isinstance(data, ConfigNode):
            return dumper.represent_data(data)

        if isinstance(data, collections.Mapping):
            if tag:
                return dumper.represent_mapping(tag, data)
            else:
                data = dict(data)
                return dumper.represent_data(data)

        elif isinstance(data, collections.Sequence) and not isinstance(data, str) and not isinstance(data, bytes):
            if tag:
                return dumper.represent_sequence(tag, data)
            else:
                if isinstance(data, collections.MutableSequence):
                    data = list(data)
                else:
                    data = tuple(data)
                return dumper.represent_data(data)
        else:
            if tag:
                if data is None:
                    assert tag.startswith('!null')
                    return dumper.represent_scalar(tag, str(''))
                return dumper.represent_scalar(tag, str(data))
            else:
                from .nodes.scalar import ConfigScalar
                if isinstance(data, ConfigScalar):
                    return dumper.represent_data(data._dyn_base(data))
                else:
                    # fallback to str
                    return dumper.represent_scalar('tag:yaml.org,2002:str', str(data))
    finally:
        if pop:
            dumper.metadata.pop()


def _none_representer(dumper, none):
    return dumper.dump_scalar('!null', str(''))


yaml.add_multi_representer(ConfigNode, _node_representer)
yaml.add_representer(type(None), _none_representer)

def _get_metadata_end(data, beg):
    _beg = beg+2
    def _readline():
        nonlocal _beg
        end = data.find('}}', _beg)
        if end == -1:
            end = len(data)
        else:
            end += 2

        ret = data[_beg:end]
        _beg = end
        return ret.encode('utf8')
         
    last_close = False
    end = None
    for token in tokenize.tokenize(_readline):
        if token.type == 53 and token.string == '}':
            if last_close:
                end = _beg
                break
            else:
                last_close = True
        else:
            last_close = False

    return end


def _get_metadata_content(data):
    _metadata_tag = '!metadata'
    curr_pos = data.find(_metadata_tag)
    while curr_pos != -1:
        beg = curr_pos + len(_metadata_tag)
        if data[beg] != ':':
            if data[beg:beg+2] != '{{':
                raise ValueError(f'Metadata tag should be followed by "{{{{" at character: {curr_pos}')
            end = _get_metadata_end(data, beg)
            if end is None:
                raise ValueError(f'Cannot find the end of a !metadata node which begins at: {curr_pos}')
            
            yield beg, end

        curr_pos = data.find(_metadata_tag, end+1)


def _encode_all_metadata(data):
    ranges = list(_get_metadata_content(data))
    offset = 0
    for beg, end in ranges:
        beg += offset
        end += offset

        metadata = eval(data[beg+1:end-1])
        encoded = _encode_metadata(metadata)
        repl = ':' + encoded
        orig_len = end-beg
        repl_len = len(repl)
        data = data[:beg] + repl + data[end:]
        offset += repl_len - orig_len

    return data


def parse(data, builder):
    if not isinstance(data, str):
        data = data.read()

    #print(data)
    data = _encode_all_metadata(data)
    def get_loader(*args, **kwargs):
        loader = yaml.Loader(*args, **kwargs)
        loader.context = builder
        if builder.get_current_file():
            loader.name = builder.get_current_file()
        return loader

    for raw in yaml.load_all(data, Loader=get_loader):
        yield ConfigNode(raw)


def dump(output, nodes, open_mode='w', exclude_metadata=None):
    close = False
    if isinstance(output, str):
        output = open(output, open_mode)
        close = True

    def get_dumper(*args, **kwargs):
        dumper = yaml.Dumper(*args, **kwargs)
        assert not hasattr(dumper, 'metadata')
        dumper.metadata = []
        dumper.exclude_metadata = exclude_metadata or set()
        return dumper

    try:
        yaml.dump(ConfigNode(nodes), stream=output, Dumper=get_dumper)
    finally:
        if close:
            output.close()
