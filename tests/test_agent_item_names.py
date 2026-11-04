"""The agent rung's answer records are named from their own structure
(``mcpserver/compiled_tools._item_name``): the field the result's own address names,
then the usual name keys, then the field whose words stand in the address. A name is cut
to the address's words only when what follows them is a version or a count.

Offline: plain records, no site, no model.
"""
import sys

import pytest

from anyuse.mcpserver import compiled_tools as CT


@pytest.mark.parametrize("item, name", [
    # the address names the result; the version after it is not part of the name
    ({"name": "rand v0.10.3", "url": "https://pkgs.test/crates/rand"}, "rand"),
    ({"title": "serde_json 1.0.128", "url": "https://pkgs.test/c/serde_json"}, "serde_json"),
    # a name that goes on with words keeps them, whatever the address starts with
    ({"name": "Randall Shop", "url": "https://shops.test/s/rand"}, "Randall Shop"),
    ({"name": "123 Main St", "url": "https://maps.test/a/123"}, "123 Main St"),
    # keys in another language: the field the address names, not the first text (a price)
    ({"preis": "12,99 €", "titel": "Zange", "link": "https://werkzeug.test/p/zange"}, "Zange"),
    ({"cena": "199 zł", "nazwa": "Wiertarka Bosch PSB 500",
      "adres": "https://sklep.test/p/wiertarka-bosch-psb-500"}, "Wiertarka Bosch PSB 500"),
    ({"giá": "250.000đ", "tên": "Nồi cơm điện",
      "đường dẫn": "https://cho.test/sp/noi-com-dien"}, "Nồi cơm điện"),
    # a field that copies the address's segment as written is no shown name: the keys decide
    ({"title": "Pizza Margherita (large)", "slug": "pizza-margherita",
      "url": "https://menu.test/p/pizza-margherita"}, "Pizza Margherita (large)"),
    # an id in the address names nothing: the usual keys decide
    ({"id": "12345", "title": "Bosch drill", "url": "https://shop.test/p/12345"}, "Bosch drill"),
    # no address: the name keys, then the first text, as before
    ({"title": "Cedar Point"}, "Cedar Point"),
    ({"naam": "Fietsbel"}, "Fietsbel"),
    ("plain text", "plain text"),
])
def test_an_answer_record_is_named_from_its_structure(item, name):
    assert CT._item_name(item) == name


def test_the_address_words_inside_a_longer_field_name_it_when_no_key_does():
    item = {"preço": "R$ 89", "produto": "Chaleira elétrica",
            "descrição": "Chaleira elétrica de inox com 1,7 litro",
            "endereço": "https://loja.test/p/chaleira-eletrica-inox-1-7"}
    assert CT._item_name(item) == "Chaleira elétrica"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
