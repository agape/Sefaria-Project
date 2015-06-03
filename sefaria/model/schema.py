# -*- coding: utf-8 -*-
import copy

import logging
logger = logging.getLogger(__name__)

try:
    import re2 as re
    re.set_fallback_notification(re.FALLBACK_WARNING)
except ImportError:
    logging.warning("Failed to load 're2'.  Falling back to 're' for regular expression parsing. See https://github.com/blockspeiser/Sefaria-Project/wiki/Regular-Expression-Engines")
    import re

import regex
from . import abstract as abst

from sefaria.system.exceptions import InputError, IndexSchemaError
from sefaria.utils.hebrew import decode_hebrew_numeral, encode_hebrew_numeral, hebrew_term


"""
                -----------------------------------------
                 Titles, Terms, and Term Schemes
                -----------------------------------------
"""


class TitleGroup(object):
    """
    A collection of titles.  Used for titles of SchemaNodes, for Maps, and for Terms
    """

    def __init__(self, serial=None):
        self.titles = []
        self._primary_title = {}
        if serial:
            self.load(serial)

    def load(self, serial=None):
        if serial:
            self.titles = serial

    def copy(self):
        return self.__class__(copy.deepcopy(self.titles))

    def primary_title(self, lang="en"):
        """
        Return the primary title for this node in the language specified
        :param lang: "en" or "he"
        :return: The primary title string or None
        """
        if self._primary_title.get(lang) is None:
            for t in self.titles:
                if t.get("lang") == lang and t.get("primary"):
                    self._primary_title[lang] = t.get("text")
                    break
        if not self._primary_title.get(lang):
            self._primary_title[lang] = ""

        return self._primary_title.get(lang)

    def all_titles(self, lang=None):
        """
        :param lang: "en" or "he"
        :return: list of strings - the titles of this node
        """
        if lang is None:
            return [t["text"] for t in self.titles]
        return [t["text"] for t in self.titles if t["lang"] == lang]

    def remove_title(self, text, lang):
        self.titles = [t for t in self.titles if not (t["lang"] == lang and t["text"] == text)]
        return self

    def add_title(self, text, lang, primary=False, replace_primary=False, presentation="combined"):
        """
        :param text: Text of the title
        :param language:  Language code of the title (e.g. "en" or "he")
        :param primary: Is this a primary title?
        :param replace_primary: must be true to replace an existing primary title
        :param presentation: The "presentation" field of a title indicates how it combines with earlier titles. Possible values:
            "combined" - in referencing this node, earlier titles nodes are prepended to this one (default)
            "alone" - this node is reference by this title alone
            "both" - this node is addressable both in a combined and a alone form.
        :return: the object
        """
        if any([t for t in self.titles if t["text"] == text and t["lang"] == lang]):  #already there
            if not replace_primary:
                return
            else:  #update this title as primary: remove it, then re-add below
                self.remove_title(text, lang)
        d = {
                "text": text,
                "lang": lang
        }

        if primary:
            d["primary"] = True

        if presentation == "alone" or presentation == "both":
            d["presentation"] = presentation

        has_primary = any([x for x in self.titles if x["lang"] == lang and x.get("primary")])
        if has_primary and primary:
            if not replace_primary:
                raise IndexSchemaError("Node {} already has a primary title.".format(self.primary_title()))

            old_primary = self.primary_title(lang)
            self.titles = [t for t in self.titles if t["lang"] != lang or not t.get("primary")]
            self.titles.append({"text": old_primary, "lang": lang})
            self._primary_title[lang] = None

        self.titles.append(d)
        return self


class Term(abst.AbstractMongoRecord):
    """
    A Term is a shared title node.  It can be referenced and used by many different Index nodes.
    Examples:  Noah, Perek HaChovel, Even HaEzer
    Terms that use the same TermScheme can be ordered.
    """
    collection = 'term'
    track_pkeys = True
    pkeys = ["name"]
    title_group = None

    required_attrs = [
        "name",
        "titles"
    ]
    optional_attrs = [
        "scheme",
        "order",
        "ref"
    ]

    def _set_derived_attributes(self):
        self.set_titles(self.titles)

    def set_titles(self, titles):
        self.title_group = TitleGroup(titles)

    def _validate(self):
        super(Term, self)._validate()
        if any((c in '-') for c in self.title_group.primary_title("en")):
            raise InputError("Primary English title may not contain hyphens.")

    def _normalize(self):
        self.titles = self.title_group.titles

    def get_titles(self, lang=None):
        return self.title_group.all_titles(lang)


class TermSet(abst.AbstractMongoSet):
    recordClass = Term


class TermScheme(abst.AbstractMongoRecord):
    """
    A TermScheme is a category of terms.
    Example: Parsha, Perek
    """
    collection = 'term_scheme'
    track_pkeys = True
    pkeys = ["name"]

    required_attrs = [
        "name"
    ]
    optional_attrs = [

    ]

    def get_terms(self):
        return TermSet({"scheme": self.name})


class TermSchemeSet(abst.AbstractMongoSet):
    recordClass = TermScheme


"""
                ---------------------------------
                 Index Schema Trees - Core Nodes
                ---------------------------------
"""


def deserialize_tree(serial=None, **kwargs):
    """
    Build a :class:`TreeNode` tree from serialized form.  Called recursively.
    :param serial: The serialized form of the subtree
    :param kwargs: keyword argument 'struct_class' specifies the class to use as the default structure node class.
    Other keyword arguments are passed through to the node constructors.
    :return: :class:`TreeNode`
    """
    klass = None
    if serial.get("nodeType"):
        try:
            klass = globals()[serial.get("nodeType")]
        except KeyError:
            raise IndexSchemaError("No matching class for nodeType {}".format(serial.get("nodeType")))

    if serial.get("nodes"):
        #Structure class - use explicitly defined 'nodeType', code overide 'struct_class', or default SchemaNode
        struct_class = klass or kwargs.get("struct_class", SchemaNode)
        return struct_class(serial, **kwargs)
    elif klass:
        return klass(serial, **kwargs)
    else:
        raise IndexSchemaError("Schema node has neither 'nodes' nor 'nodeType'")


class TreeNode(object):
    """
    A single node in a tree.
    These trees are hierarchies - each node can have 1 or 0 parents.
    """
    required_param_keys = []
    optional_param_keys = []

    def __init__(self, serial=None, **kwargs):

        self._init_defaults()
        if not serial:
            return
        self.__dict__.update(serial)
        if getattr(self, "nodes", None) is not None:
            for node in self.nodes:
                self.append(deserialize_tree(node, **kwargs))
            del self.nodes

    def _init_defaults(self):
        self.children = []  # Is this enough?  Do we need a dict for addressing?
        self.parent = None

    def validate(self):
        for k in self.required_param_keys:
            if getattr(self, k, None) is None:
                raise IndexSchemaError("Missing Parameter '{}' in {}".format(k, self.__class__.__name__))
        for c in self.children:
            c.validate()

    def append(self, node):
        """
        Append node to this node
        :param node: the node to be appended to this node
        :return:
        """
        self.children.append(node)
        node.parent = self
        return self

    def append_to(self, node):
        """
        Append this node to another node
        :param node: the node to append this node to
        :return:
        """
        node.append(self)
        return self

    def has_children(self):
        """
        :return bool: True if this node has children
        """
        return bool(self.children)

    def is_leaf(self):
        return not self.children

    def siblings(self):
        """
        :return list: The sibling nodes of this node
        """
        if self.parent:
            return [x for x in self.parent.children if x is not self]
        else:
            return None

    def root(self):
        if self.is_root():
            return self
        return self.parent.root()

    def first_child(self):
        if not self.has_children():
            return None
        return self.children[0]

    def last_child(self):
        if not self.has_children():
            return None
        return self.children[-1]

    def first_leaf(self):
        if self.is_leaf():
            return self
        return self.first_child().first_leaf()

    def last_leaf(self):
        if self.is_leaf():
            return self
        return self.last_child().last_leaf()

    def _prev_in_list(self, l):
        if not self.parent:
            return None
        prev = None
        for x in l:
            if x is self:
                return prev
            prev = x

    def _next_in_list(self, l):
        match = False
        for x in l:
            if match:
                return x
            if x is self:
                match = True
                continue
        return None

    def prev_sibling(self):
        if not self.parent:
            return None
        return self._prev_in_list(self.parent.children)

    def next_sibling(self):
        if not self.parent:
            return None
        return self._next_in_list(self.parent.children)

    #Currently assumes being called from leaf node - could integrate a call to first_leaf/last_leaf
    def next_leaf(self):
        return self._next_in_list(self.root().get_leaf_nodes())

    #Currently assumes being called from leaf node - could integrate a call to first_leaf/last_leaf
    def prev_leaf(self):
        return self._prev_in_list(self.root().get_leaf_nodes())

    def is_root(self):
        return not self.parent

    def is_flat(self):
        """
        Is this node a flat tree, with no parents or children?
        :return bool:
        """
        return not self.parent and not self.children

    def serialize(self, **kwargs):
        d = {}
        if self.has_children():
            d["nodes"] = []
            for n in self.children:
                d["nodes"].append(n.serialize(**kwargs))

        #Only output nodeType and nodeParameters if there is at least one param. This seems like it may not remain a good measure.
        params = {k: getattr(self, k) for k in self.required_param_keys + self.optional_param_keys if getattr(self, k, None) is not None}
        if any(params):
            d["nodeType"] = self.__class__.__name__
            d.update(params)


        return d

    def copy(self, callback=None):
        children_serial = []
        for child in self.children:
            children_serial.append(child.copy(callback).serialize())
        serial = copy.deepcopy(self.serialize())
        if "nodes" in serial:
            serial["nodes"] = children_serial
        new_node = self.__class__(serial)
        if callback:
            new_node = callback(new_node)
        return new_node

    def get_leaf_nodes(self):
        if self.is_leaf():
            return [self]
        else:
            nodes = []
            for node in self.children:
                nodes += node.get_leaf_nodes()
            return nodes


class TitledTreeNode(TreeNode):
    """
    A tree node that has a collection of titles - as contained in a TitleGroup instance.
    In this class, node titles, terms, 'default', and combined titles are handled.
    """
    after_title_delimiter_re = ur"[,.: \r\n]+"  # should be an arg?  \r\n are for html matches
    title_separators = [u" ", u", "]

    def __init__(self, serial=None, **kwargs):
        super(TitledTreeNode, self).__init__(serial, **kwargs)

        if getattr(self, "titles", None):
            self.title_group.load(serial=self.titles)
            del self.__dict__["titles"]

        self._process_terms()

    def _init_defaults(self):
        super(TitledTreeNode, self)._init_defaults()
        self.default = False
        self._primary_title = {}
        self._full_title = {}
        self._full_titles = {}

        self._init_titles()
        self.sharedTitle = None

    def _init_titles(self):
        self.title_group = TitleGroup()

    def _process_terms(self):
        if self.sharedTitle:
            try:
                term = Term().load({"name": self.sharedTitle})
                self.title_group = term.title_group
            except Exception, e:
                raise IndexSchemaError("Failed to load term named {}. {}".format(self.sharedTitle, e))

    def all_tree_titles(self, lang="en"):
        """
        :param lang: "en" or "he"
        :return: list of strings - all possible titles within this subtree
        """
        return self.title_dict(lang).keys()

    def title_dict(self, lang="en", baselist=[]):
        """
        Recursive function that generates a map from title to node
        :param node: the node to start from
        :param lang: "en" or "he"
        :param baselist: list of starting strings that lead to this node
        :return: map from title to node
        """
        title_dict = {}
        thisnode = self

        this_node_titles = [title["text"] for title in self.get_titles() if title["lang"] == lang and title.get("presentation") != "alone"]
        if baselist:
            node_title_list = [baseName + sep + title for baseName in baselist for sep in self.title_separators for title in this_node_titles]
        else:
            node_title_list = this_node_titles

        alone_node_titles = [title["text"] for title in self.get_titles() if title["lang"] == lang and title.get("presentation") == "alone" or title.get("presentation") == "both"]
        node_title_list += alone_node_titles

        if self.has_children():
            for child in self.children:
                if child.is_default():
                    thisnode = child
                else:
                    title_dict.update(child.title_dict(lang, node_title_list))

        for title in node_title_list:
            title_dict[title] = thisnode

        return title_dict

    def full_titles(self, lang="en"):
        if not self._full_titles.get(lang):
            if self.parent:
                self._full_titles[lang] = [parent + sep + local
                                           for parent in self.parent.full_titles(lang)
                                           for sep in self.title_separators
                                           for local in self.all_node_titles(lang)]
            else:
                self._full_titles[lang] = self.all_node_titles(lang)
        return self._full_titles[lang]

    def full_title(self, lang="en"):
        """
        :param lang: "en" or "he"
        :return string: The full title of this node, from the root node.
        """
        if not self._full_title.get(lang):
            if self.parent:
                self._full_title[lang] = self.parent.full_title(lang) + ", " + self.primary_title(lang)
            else:
                self._full_title[lang] = self.primary_title(lang)
        return self._full_title[lang]

    def is_default(self):
        """
        Is this node a default node, meaning, do references to its parent cascade to this node?
        :return bool:
        """
        return self.default

    def has_titled_continuation(self):
        """
        :return: True if any normal forms of this node continue with a title.  Used in regex building.
        """
        return any([c for c in self.children if not c.is_default()])

    def has_numeric_continuation(self):
        """
        True if any of the normal forms of this node continue with numbers.  Used in regex building.
        Overriden in subclasses.
        :return:
        """
        #overidden in subclasses
        for child in self.children:
            if child.is_default():
                if child.has_numeric_continuation():
                    return True
        return False

    def get_titles(self):
        return getattr(self.title_group, "titles", None)

    def primary_title(self, lang="en"):
        """
        Return the primary title for this node in the language specified
        :param lang: "en" or "he"
        :return: The primary title string or None
        """
        return self.title_group.primary_title(lang)

    def all_node_titles(self, lang="en"):
        """
        :param lang: "en" or "he"
        :return: list of strings - the titles of this node
        """
        return self.title_group.all_titles(lang)

    def remove_title(self, text, lang):
        return self.title_group.remove_title(text, lang)

    def add_title(self, text, lang, primary=False, replace_primary=False, presentation="combined"):
        """
        :param text: Text of the title
        :param language:  Language code of the title (e.g. "en" or "he")
        :param primary: Is this a primary title?
        :param replace_primary: must be true to replace an existing primary title
        :param presentation: The "presentation" field of a title indicates how it combines with earlier titles. Possible values:
            "combined" - in referencing this node, earlier titles nodes are prepended to this one (default)
            "alone" - this node is reference by this title alone
            "both" - this node is addressable both in a combined and a alone form.
        :return: the object
        """
        return self.title_group.add_title(text, lang, primary, replace_primary, presentation)

    def add_shared_term(self, term):
        self.sharedTitle = term
        self._process_terms()

    def validate(self):
        super(TitledTreeNode, self).validate()

        if any((c in '-') for c in self.title_group.primary_title("en")):
            raise InputError("Primary English title may not contain hyphens.")

        if not self.default and not self.sharedTitle and not self.get_titles():
            raise IndexSchemaError("Schema node {} must have titles, a shared title node, or be default".format(self))

        if self.default and (self.get_titles() or self.sharedTitle):
            raise IndexSchemaError("Schema node {} - default nodes can not have titles".format(self))

        if not self.default and not self.primary_title("en"):
            raise IndexSchemaError("Schema node {} missing primary English title".format(self))

        if self.has_children() and len([c for c in self.children if c.default]) > 1:
            raise IndexSchemaError("Schema Structure Node {} has more than one default child.".format(self.key))

        if self.sharedTitle and Term().load({"name": self.sharedTitle}).titles != self.get_titles():
            raise IndexSchemaError("Schema node {} with sharedTitle can not have explicit titles".format(self))

        #if not self.default and not self.primary_title("he"):
        #    raise IndexSchemaError("Schema node {} missing primary Hebrew title".format(self.key))

    def serialize(self, **kwargs):
        d = super(TitledTreeNode, self).serialize(**kwargs)
        if self.default:
            d["default"] = True
        else:
            if self.sharedTitle:
                d["sharedTitle"] = self.sharedTitle
            if not self.sharedTitle or kwargs.get("expand_shared"):
                d["titles"] = self.get_titles()
        if kwargs.get("expand_titles"):
            d["title"] = self.title_group.primary_title("en")
            d["heTitle"] = self.title_group.primary_title("he")
        return d

    """ String Representations """
    def __str__(self):
        return self.full_title("en")

    def __repr__(self):  # Wanted to use orig_tref, but repr can not include Unicode
        return self.__class__.__name__ + "('" + self.full_title("en") + "')"


"""
                ---------------------------------------
                 Alternate Structure Tree Nodes (maps)
                ---------------------------------------
"""


class NumberedTitledTreeNode(TitledTreeNode):
    """
    A :class:`TreeNode` that can address its :class:`TreeNode` children by Integer, or other :class:`AddressType`.
    """
    required_param_keys = ["depth", "addressTypes", "sectionNames"]
    optional_param_keys = ["lengths"]

    def __init__(self, serial=None, **kwargs):
        """
        depth: Integer depth of this JaggedArray
        address_types: A list of length (depth), with string values indicating class names for address types for each level
        section_names: A list of length (depth), with string values of section names for each level
        e.g.:
        {
          "depth": 2,
          "addressTypes": ["Integer","Integer"],
          "sectionNames": ["Chapter","Verse"],
          "lengths": [12, 122]
        }
        """
        super(NumberedTitledTreeNode, self).__init__(serial, **kwargs)
        self._init_address_classes()

    def _init_address_classes(self):
        self._addressTypes = []
        for i, atype in enumerate(getattr(self, "addressTypes", [])):
            try:
                klass = globals()["Address" + atype]
            except KeyError:
                raise IndexSchemaError("No matching class for addressType {}".format(atype))

            if i == 0 and getattr(self, "lengths", None) and len(self.lengths) > 0:
                self._addressTypes.append(klass(i, self.lengths[i]))
            else:
                self._addressTypes.append(klass(i))

    def validate(self):
        super(NumberedTitledTreeNode, self).validate()
        for p in ["addressTypes", "sectionNames"]:
            if len(getattr(self, p)) != self.depth:
                raise IndexSchemaError("Parameter {} in {} {} does not have depth {}".format(p, self.__class__.__name__, self.key, self.depth))

    def address_class(self, depth):
        return self._addressTypes[depth]

    # todo: accept 'anchored' arguement, and return Regex object.
    def full_regex(self, title, lang, anchored=True, **kwargs):
        """
        :return: Regex object. If for_js == True, returns the Regex string

        A call to `full_regex("Bereishit", "en", for_js=True)` returns the follow regex, expanded here for clarity :
        ```
        Bereishit                       # title
        [,.: \r\n]+                     # a separator (self.after_title_delimiter_re)
        (?:                             # Either:
            (?:                         # 1)
                (\d+)                   # Digits
                (                       # and maybe
                    [,.: \r\n]+         # a separator
                    (\d+)               # and more digits
                )?
            )
            |                           # Or:
            (?:                         # 2: The same
                [[({]                   # With beginning
                (\d+)
                (
                    [,.: \r\n]+
                    (\d+)
                )?
                [])}]                   # and ending brackets or parens or braces around the numeric portion
            )
        )
        (?=                             # and then either
            [.,;?! })<]                 # some kind of delimiting character coming after
            |                           # or
            $                           # the end of the string
        )
        ```
        Different address type / language combinations produce different internal regexes in the innermost portions of the above, where the comments say 'digits'.

        """
        reg = ur"^" if anchored else ""
        reg += regex.escape(title) + self. after_title_delimiter_re
        reg += ur'(?:(?:' + self.address_regex(lang, **kwargs) + ur')|(?:[\[({]' + self.address_regex(lang, **kwargs) + ur'[\])}]))'  # Match expressions with internal parenthesis around the address portion
        reg += ur"(?=\W|$)" if not kwargs.get("for_js") else ur"(?=[.,;?! })<]|$)"  #Include : in list of ending chars?
        return regex.compile(reg) if not kwargs.get("for_js") else reg

    def address_regex(self, lang, **kwargs):
        group = "a0" if not kwargs.get("for_js") else None
        reg = self._addressTypes[0].regex(lang, group, **kwargs)

        if not self._addressTypes[0].stop_parsing(lang):
            for i in range(1, self.depth):
                group = "a{}".format(i) if not kwargs.get("for_js") else None
                reg += u"(" + self.after_title_delimiter_re + self._addressTypes[i].regex(lang, group, **kwargs) + u")"
                if not kwargs.get("strict", False):
                    reg += u"?"

        return reg

    def sectionString(self, sections, lang="en", title=True, full_title=False):
        assert len(sections) <= self.depth

        ret = u""
        if title:
            ret += self.full_title(lang) if full_title else self.primary_title(lang)
            ret += u" "
        strs = []
        for i in range(len(sections)):
            strs.append(self.address_class(i).toStr(lang, sections[i]))
        ret += u":".join(strs)

        return ret

    def serialize(self, **kwargs):
        d = super(NumberedTitledTreeNode, self).serialize(**kwargs)
        if kwargs.get("translate_sections"):
                d["heSectionNames"] = map(hebrew_term, self.sectionNames)
        return d


class ArrayMapNode(NumberedTitledTreeNode):
    """
    A :class:`TreeNode` that contains jagged arrays of references.
    Used as the leaf node of alternate structures of Index records.
    (e.g., Parsha structures of chapter/verse stored Tanach, or Perek structures of Daf/Line stored Talmud)
    """
    #Is there a better way to inherit these from the super?
    required_param_keys = ["depth", "addressTypes", "sectionNames", "wholeRef", "refs"]
    optional_param_keys = ["lengths"]
    has_key = False  # This is not used as schema for content

    def get_ref_from_sections(self, sections):
        if not sections:
            return self.wholeRef
        return reduce(lambda a, i: a[i], [s - 1 for s in sections], self.refs)

    def serialize(self, **kwargs):
        d = super(ArrayMapNode, self).serialize(**kwargs)
        if kwargs.get("expand_refs"):

            def expand_ref(tref):
                from . import text
                from sefaria.utils.util import text_preview

                oref = text.Ref(tref)
                if oref.is_spanning():
                    oref = oref.split_spanning_ref()[0]
                t = text.TextFamily(oref, context=0, pad=False, commentary=False)
                preview = text_preview(t.text, t.he) if (t.text or t.he) else []

                return preview

            d["wholeRefPreview"] = expand_ref(self.wholeRef)
            d["refsPreview"] = map(expand_ref, self.refs)
        return d
"""
                -------------------------
                 Index Schema Tree Nodes
                -------------------------
"""


class SchemaNode(TitledTreeNode):
    """
    A node in an Index Schema tree.
    Schema nodes form trees which define a storage format.
    At this level, keys, storage addresses, and recursive content constructors are defined.
    Conceptually, there are two types of Schema node:
    - Schema Structure Nodes define nodes which have child nodes, and do not store content.
    - Schema Content Nodes define nodes which store content, and do not have child nodes
    The two are both handled by this class, with calls to "if self.children" to distinguishing behavior.

    """

    def __init__(self, serial=None, **kwargs):
        """
        Construct a SchemaNode
        :param index: The Index object that this tree is rooted in.
        :param serial: The serialized form of this subtree
        :return:
        """
        super(SchemaNode, self).__init__(serial, **kwargs)
        self.index = kwargs.get("index", None)

    def _init_defaults(self):
        super(SchemaNode, self)._init_defaults()
        self.key = None
        self.checkFirst = None
        self._address = []

    def validate(self):
        super(SchemaNode, self).validate()

        if not getattr(self, "key", None):
            raise IndexSchemaError("Schema node missing key")

        if self.default and self.key != "default":
            raise IndexSchemaError("'default' nodes need to have key name 'default'")

    def traverse_to_string(self, callback, depth=0, **kwargs):
        st = callback(self, depth, **kwargs)
        for child in self.children:
            st += child.traverse_to_string(callback, depth + 1, **kwargs)
        return st

    def create_content(self, callback=None, *args, **kwargs):
        """
        Tree visitor for building content trees based on this Index tree - used for counts and versions
        Callback is called for content nodes only.
        :param callback:
        :return:
        """
        if self.children:
            return {node.key: node.create_content(callback, *args, **kwargs) for node in self.children}
        else:
            if not callback:
                return None
            return callback(self, *args, **kwargs)

    def create_skeleton(self):
        return self.create_content(lambda n: [])

    def visit_content(self, callback, *contents, **kwargs):
        """
        Tree visitor for traversing content nodes of existing content trees based on this Index tree and passing them to callback.
        Outputs a content tree.
        Callback is called for content nodes only.
        :param contents: one tree or many
        :param callback:
        :return:
        """
        if self.children:
            dict = {}
            for node in self.children:
                # todo: abstract out or put in helper the below reduce
                c = [tree[node.key] for tree in contents]
                dict[node.key] = node.visit_content(callback, *c, **kwargs)
            return dict
        else:
            return self.create_content(callback, *contents, **kwargs)

    def visit_structure(self, callback, content, **kwargs):
        """
        Tree visitor for traversing existing structure nodes of content trees based on this Index and passing them to callback.
        Traverses from bottom up, with intention that this be used to aggregate content from content nodes up.
        Modifies contents in place.
        :param callback:
        :param args:
        :param kwargs:
        :return:
        """
        if self.children:
            for node in self.children:
                node.visit_structure(callback, content)
            callback(self, content.content_node(self), **kwargs)

    def as_index_contents(self):
        res = self.index.contents(raw=True)
        res["title"]   = self.full_title("en")
        res["heTitle"] = self.full_title("he")
        res['schema']  = self.serialize(expand_shared=True, expand_titles=True, translate_sections=True)
        res["titleVariants"] = self.full_titles("en")
        if self.all_node_titles("he"):
            res["heTitleVariants"] = self.full_titles("he")
        if self.index.has_alt_structures():
            res['alts'] = {}
            for key, struct in self.index.get_alt_structures().iteritems():
                res['alts'][key] = struct.serialize(expand_shared=True, expand_refs=True, expand_titles=True)
            del res['alt_structs']
        return res

    def serialize(self, **kwargs):
        """
        :param callback: function applied to dictionary before it's returned.  Invoked on concrete nodes, not the abstract level.
        :return string: serialization of the subtree rooted in this node
        """
        d = super(SchemaNode, self).serialize(**kwargs)
        d["key"] = self.key
        if self.checkFirst:
            d["checkFirst"] = self.checkFirst
        return d

    #http://stackoverflow.com/a/14692747/213042
    #http://stackoverflow.com/a/16300379/213042
    def address(self):
        """
        Returns a list of keys to uniquely identify and to access this node.
        :return list:
        """
        if not self._address:
            if self.parent:
                self._address = self.parent.address() + [self.key]
            else:
                self._address = [self.key]

        return self._address

    def version_address(self):
        """
        In a version storage context, the first key is not used.  Traversal starts from position 1.
        :return:
        """
        return self.address()[1:]

    def ref(self):
        from . import text
        d = {
            "index": self.index,
            "book": self.full_title("en"),
            "type": self.index.categories[0],
            "index_node": self,
            "sections": [],
            "toSections": []
        }
        return text.Ref(_obj=d)

    def first_section_ref(self):
        if not self.is_leaf():
            return self.ref()

        return self.ref().padded_ref()

    def last_section_ref(self):
        if not self.is_leaf():
            return self.ref()

        from . import version_state
        from . import text

        sn = version_state.StateNode(snode=self)
        sections = [i + 1 for i in sn.ja("all").last_index(self.depth - 1)]

        d = self.ref()._core_dict()
        d["sections"] = sections
        d["toSections"] = sections
        return text.Ref(_obj=d)

    def __eq__(self, other):
        return self.address() == other.address()

    def __ne__(self, other):
        return not self.__eq__(other)


class JaggedArrayNode(SchemaNode, NumberedTitledTreeNode):
    """
    A :class:`SchemaNode` that defines JaggedArray content and can be addressed by :class:`AddressType`
    Used both for:
    - Structure Nodes whose children can be addressed by Integer or other :class:`AddressType`
    - Content Nodes that define the schema for JaggedArray stored content
    """
    def __init__(self, serial=None, **kwargs):
        # call SchemaContentNode.__init__, then the additional parts from NumberedTitledTreeNode.__init__
        super(JaggedArrayNode, self).__init__(serial, **kwargs)
        self._init_address_classes()

    def validate(self):
        # this is minorly repetitious, at the top tip of the diamond inheritance.
        SchemaNode.validate(self)
        NumberedTitledTreeNode.validate(self)

    def has_numeric_continuation(self):
        return True

    def as_index_contents(self):
        res = super(JaggedArrayNode, self).as_index_contents()
        res["sectionNames"] = self.sectionNames
        res["depth"] = self.depth
        return res


class StringNode(JaggedArrayNode):
    """
    A :class:`JaggedArrayNode` with depth 0 - effectively defining a string.
    """
    def __init__(self, serial=None, **kwargs):
        super(StringNode, self).__init__(serial, **kwargs)
        self.depth = 0
        self.addressTypes = []
        self.sectionNames = []

    def serialize(self, **kwargs):
        d = super(StringNode, self).serialize(**kwargs)
        d["nodeType"] = "JaggedArrayNode"
        return d


"""
                ------------------------------------
                 Index Schema Trees - Address Types
                ------------------------------------
"""


class AddressType(object):
    """
    Defines a scheme for referencing and addressing a level of a Jagged Array.
    Used by :class:`NumberedTitledTreeNode`
    """
    section_patterns = {
        'he': None,
        'en': None
    }

    def __init__(self, order, length=None):
        self.order = order
        self.length = length

    def regex(self, lang, group_id=None, **kwargs):
        """
        The regular expression part that matches this address reference, wrapped with section names, if provided
        :param lang: "en" or "he"
        :param group_id: The id of the regular expression group the this match will be catured in
        :param kwargs: 'strict' kwarg indicates that section names are required to match
        :return string: regex component
        """
        try:
            if self.section_patterns[lang]:
                strict = kwargs.get("strict", False)
                reg = self.section_patterns[lang]
                if not strict:
                    reg += u"?"
                reg += self._core_regex(lang, group_id)
                return reg
            else:
                return self._core_regex(lang, group_id)
        except KeyError:
            raise Exception("Unknown Language passed to AddressType: {}".format(lang))

    def _core_regex(self, lang, group_id=None):
        """
        The regular expression part that matches this address reference
        :param lang: "en" or "he"
        :param group_id: The id of the regular expression group the this match will be catured in
        :return string: regex component
        """
        pass

    @staticmethod
    def hebrew_number_regex():
        """
        Regular expression component to capture a number expressed in Hebrew letters
        :return string:
        \p{Hebrew} ~= [\u05d0–\u05ea]
        """
        return ur"""                                    # 1 of 3 styles:
        ((?=[\u05d0-\u05ea]+(?:"|\u05f4|'')[\u05d0-\u05ea])    # (1: ") Lookahead:  At least one letter, followed by double-quote, two single quotes, or gershayim, followed by  one letter
                \u05ea*(?:"|\u05f4|'')?				    # Many Tavs (400), maybe dbl quote
                [\u05e7-\u05ea]?(?:"|\u05f4|'')?	    # One or zero kuf-tav (100-400), maybe dbl quote
                [\u05d8-\u05e6]?(?:"|\u05f4|'')?	    # One or zero tet-tzaddi (9-90), maybe dbl quote
                [\u05d0-\u05d8]?					    # One or zero alef-tet (1-9)															#
            |(?=[\u05d0-\u05ea])						    # (2: no punc) Lookahead: at least one Hebrew letter
                \u05ea*								    # Many Tavs (400)
                [\u05e7-\u05ea]?					    # One or zero kuf-tav (100-400)
                [\u05d8-\u05e6]?					    # One or zero tet-tzaddi (9-90)
                [\u05d0-\u05d8]?					    # One or zero alef-tet (1-9)
            |[\u05d0-\u05ea]['\u05f3]					    # (3: ') single letter, followed by a single quote or geresh
        )"""

    def stop_parsing(self, lang):
        """
        If this is true, the regular expression will stop parsing at this address level for this language
        :param lang: "en" or "he"
        :return bool:
        """
        return False

    def toNumber(self, lang, s):
        """
        Return the numerical form of s in this address scheme
        :param s: The address component
        :param lang: "en" or "he"
        :return int:
        """
        pass

    def toIndex(self, lang, s):
        return self.toNumber(lang, s) - 1

    def format_count(self, name, number):
        return {name: number}

    @staticmethod
    def toStr(lang, i):
        if lang == "en":
            return str(i)
        elif lang == "he":
            return encode_hebrew_numeral(i)


class AddressTalmud(AddressType):
    """
    :class:`AddressType` for Talmud style Daf + Amud addresses
    """
    section_patterns = {
        "en": None,
        "he": ur"(\u05d3[\u05e3\u05e4\u05f3']\s+)"			# Daf, spelled with peh, peh sofit, geresh, or single quote
    }

    def _core_regex(self, lang, group_id=None):
        if group_id:
            reg = ur"(?P<" + group_id + ur">"
        else:
            reg = ur"("

        if lang == "en":
            reg += ur"\d+[ab]?)"
        elif lang == "he":
            reg += self.hebrew_number_regex() + ur'''([.:]|[,\s]+(?:\u05e2(?:"|\u05f4|''))?[\u05d0\u05d1])?)'''

        return reg

    def stop_parsing(self, lang):
        if lang == "he":
            return True
        return False

    def toNumber(self, lang, s):
        if lang == "en":
            try:
                if s[-1] in ["a", "b"]:
                    amud = s[-1]
                    daf = int(s[:-1])
                else:
                    amud = "a"
                    daf = int(s)
            except ValueError:
                raise InputError(u"Couldn't parse Talmud reference: {}".format(s))

            if self.length and daf > self.length:
                #todo: Catch this above and put the book name on it.  Proably change Exception type.
                raise InputError(u"{} exceeds max of {} dafs.".format(daf, self.length))

            indx = daf * 2
            if amud == "a":
                indx -= 1
            return indx
        elif lang == "he":
            num = re.split("[.:,\s]", s)[0]
            daf = decode_hebrew_numeral(num) * 2
            if s[-1] == ":" or (
                    s[-1] == u"\u05d1"    #bet
                        and
                    ((len(s) > 2 and s[-2] in ", ")  # simple bet
                     or (len(s) > 4 and s[-3] == u'\u05e2')  # ayin"bet
                     or (len(s) > 5 and s[-4] == u"\u05e2")  # ayin''bet
                    )
            ):
                return daf  # amud B
            return daf - 1

            #if s[-1] == "." or (s[-1] == u"\u05d0" and len(s) > 2 and s[-2] in ",\s"):

    @staticmethod
    def toStr(lang, i):
        i += 1
        daf = i / 2

        if lang == "en":
            if i > daf * 2:
                daf = "%db" % daf
            else:
                daf = "%da" % daf

        elif lang == "he":
            if i > daf * 2:
                daf = ("%s " % encode_hebrew_numeral(daf)) + u"\u05D1"
            else:
                daf = ("%s " % encode_hebrew_numeral(daf)) + u"\u05D0"

        return daf

    def format_count(self, name, number):
        if name == "Daf":
            return {
                "Amud": number,
                "Daf": number / 2
            }
        else: #shouldn't get here
            return {name: number}


class AddressInteger(AddressType):
    """
    :class:`AddressType` for Integer addresses
    """
    def _core_regex(self, lang, group_id=None):
        if group_id:
            reg = ur"(?P<" + group_id + ur">"
        else:
            reg = ur"("

        if lang == "en":
            reg += ur"\d+)"
        elif lang == "he":
            reg += self.hebrew_number_regex() + ur")"

        return reg

    def toNumber(self, lang, s):
        if lang == "en":
            return int(s)
        elif lang == "he":
            return decode_hebrew_numeral(s)

    def toStr(self, lang, i):
        if lang == "en":
            return str(i)
        if lang == "he":
            return encode_hebrew_numeral(i)


class AddressAliyah(AddressInteger):
    en_map = [u"First", u"Second", u"Third", u"Fourth", u"Fifth", u"Sixth", u"Seventh"]
    he_map = [u"ראשון", u"שני", u"שלישי", u"רביעי", u"חמישי", u"שישי", u"שביעי"]

    def toStr(self, lang, i):
        if lang == "en":
            return self.en_map[i - 1]
        if lang == "he":
            return self.he_map[i - 1]


class AddressPerek(AddressInteger):
    section_patterns = {
        "en": ur"""(?:(?:Chapter|chapter|Perek|perek)\s*)""",
        "he": ur"""(?:
            \u05e4(?:"|\u05f4|'')?                  # Peh (for 'perek') maybe followed by a quote of some sort
            |\u05e4\u05e8\u05e7\s*                  # or 'perek' spelled out, followed by space
        )"""
    }


class AddressMishnah(AddressInteger):
    section_patterns = {
        "en": None,
        "he": ur"""(?:
            (?:\u05de\u05e9\u05e0\u05d4\s)			# Mishna spelled out, with a space after
            |(?:\u05de(?:"|\u05f4|'')?)				# or Mem (for 'mishna') maybe followed by a quote of some sort
        )"""
    }