"""Utilitas heksadesimal kecil untuk log EVM."""


def topic_addr(topic) -> str:
    """Ambil alamat (20 byte terakhir) dari sebuah topic log → ``0x…``.

    Menerima HexBytes, bytes, atau string. Selalu mengembalikan huruf kecil.
    """
    if hasattr(topic, "hex"):
        h = topic.hex()
    elif isinstance(topic, (bytes, bytearray)):
        h = topic.hex()
    else:
        h = str(topic or "")
    h = h.lower()
    if not h.startswith("0x"):
        h = "0x" + h
    return "0x" + h[-40:]
