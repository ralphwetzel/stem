# Copyright 2018, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Messages communicated over a Tor relay's ORPort.

.. versionadded:: 1.7.0

**Module Overview:**

::

  Cell - Base class for ORPort messages.
    |- CircuitCell - Circuit management.
    |  |- CreateCell - Create a circuit.              (section 5.1)
    |  |- CreatedCell - Acknowledge create.           (section 5.1)
    |  |- RelayCell - End-to-end data.                (section 6.1)
    |  |- DestroyCell - Stop using a circuit.         (section 5.4)
    |  |- CreateFastCell - Create a circuit, no PK.   (section 5.1)
    |  |- CreatedFastCell - Circuit created, no PK.   (section 5.1)
    |  |- RelayEarlyCell - End-to-end data; limited.  (section 5.6)
    |  |- Create2Cell - Extended CREATE cell.         (section 5.1)
    |  +- Created2Cell - Extended CREATED cell.       (section 5.1)
    |
    |- PaddingCell - Padding negotiation.             (section 7.2)
    |- VersionsCell - Negotiate proto version.        (section 4)
    |- NetinfoCell - Time and address info.           (section 4.5)
    |- PaddingNegotiateCell - Padding negotiation.    (section 7.2)
    |- VPaddingCell - Variable-length padding.        (section 7.2)
    |- CertsCell - Relay certificates.                (section 4.2)
    |- AuthChallengeCell - Challenge value.           (section 4.3)
    |- AuthenticateCell - Client authentication.      (section 4.5)
    |- AuthorizeCell - Client authorization.          (not yet used)
    |
    |- pack - encodes cell into bytes
    |- unpack - decodes series of cells
    +- pop - decodes cell with remainder
"""

import datetime
import inspect
import io
import os
import random
import sys

import stem.client

from stem import UNDEFINED
from stem.client import ZERO, Address, Size, split
from stem.util import _hash_attr, datetime_to_unix

FIXED_PAYLOAD_LEN = 509
AUTH_CHALLENGE_SIZE = 32
HASH_LEN = 20


class Cell(object):
  """
  Metadata for ORPort cells.
  """

  NAME = 'UNKNOWN'
  VALUE = -1
  IS_FIXED_SIZE = False

  @staticmethod
  def by_name(name):
    """
    Provides cell attributes by its name.

    :parm str name: cell command to fetch

    :raise: **ValueError** if cell type is invalid
    """

    for _, cls in inspect.getmembers(sys.modules[__name__]):
      if name == getattr(cls, 'NAME', UNDEFINED):
        return cls

    raise ValueError("'%s' isn't a valid cell type" % name)

  @staticmethod
  def by_value(value):
    """
    Provides cell attributes by its value.

    :parm int value: cell value to fetch

    :raise: **ValueError** if cell type is invalid
    """

    for _, cls in inspect.getmembers(sys.modules[__name__]):
      if value == getattr(cls, 'VALUE', UNDEFINED):
        return cls

    raise ValueError("'%s' isn't a valid cell value" % value)

  def pack(self, link_version):
    raise NotImplementedError('Unpacking not yet implemented for %s cells' % type(self).NAME)

  @staticmethod
  def unpack(content, link_version):
    """
    Unpacks all cells from a response.

    :param bytes content: payload to decode
    :param int link_version: link protocol version

    :returns: :class:`~stem.client.cell.Cell` generator

    :raises:
      * ValueError if content is malformed
      * NotImplementedError if unable to unpack any of the cell types
    """

    while content:
      cell, content = Cell.pop(content, link_version)
      yield cell

  @staticmethod
  def pop(content, link_version):
    """
    Unpacks the first cell.

    :param bytes content: payload to decode
    :param int link_version: link protocol version

    :returns: (:class:`~stem.client.cell.Cell`, remainder) tuple

    :raises:
      * ValueError if content is malformed
      * NotImplementedError if unable to unpack this cell type
    """

    circ_id, content = Size.SHORT.pop(content) if link_version < 4 else Size.LONG.pop(content)
    command, content = Size.CHAR.pop(content)
    cls = Cell.by_value(command)

    if cls.IS_FIXED_SIZE:
      payload_len = FIXED_PAYLOAD_LEN
    else:
      payload_len, content = Size.SHORT.pop(content)

    if len(content) < payload_len:
      raise ValueError('%s cell should have a payload of %i bytes, but only had %i' % (cls.NAME, payload_len, len(content)))

    payload, content = split(content, payload_len)
    return cls._unpack(payload, circ_id, link_version), content

  @classmethod
  def _pack(cls, link_version, payload, circ_id = 0):
    """
    Provides bytes that can be used on the wire for these cell attributes.
    Format of a properly packed cell depends on if it's fixed or variable
    sized...

    ::

      Fixed:    [ CircuitID ][ Command ][ Payload ][ Padding ]
      Variable: [ CircuitID ][ Command ][ Size ][ Payload ]

    :param str name: cell command
    :param int link_version: link protocol version
    :param bytes payload: cell payload
    :param int circ_id: circuit id, if a CircuitCell

    :return: **bytes** with the encoded payload

    :raise: **ValueError** if cell type invalid or payload is too large
    """

    if isinstance(cls, CircuitCell) and circ_id is None:
      if cls.NAME.startswith('CREATE'):
        # Since we're initiating the circuit we pick any value from a range
        # that's determined by our link version.

        circ_id = 0x80000000 if link_version > 3 else 0x01
      else:
        raise ValueError('%s cells require a circ_id' % cls.NAME)

    cell = io.BytesIO()
    cell.write(Size.LONG.pack(circ_id) if link_version > 3 else Size.SHORT.pack(circ_id))
    cell.write(Size.CHAR.pack(cls.VALUE))
    cell.write(b'' if cls.IS_FIXED_SIZE else Size.SHORT.pack(len(payload)))
    cell.write(payload)

    # pad fixed sized cells to the required length

    if cls.IS_FIXED_SIZE:
      cell_size = cell.seek(0, io.SEEK_END)
      fixed_cell_len = 514 if link_version > 3 else 512

      if cell_size > fixed_cell_len:
        raise ValueError('Payload of %s is too large (%i bytes), must be less than %i' % (cls.NAME, cell_size, fixed_cell_len))

      cell.write(ZERO * (fixed_cell_len - cell_size))

    return cell.getvalue()

  @classmethod
  def _unpack(cls, content, circ_id, link_version):
    """
    Subclass implementation for unpacking cell content.

    :param bytes content: payload to decode
    :param int link_version: link protocol version
    :param int circ_id: circuit id cell is for

    :returns: instance of this cell type

    :raises: **ValueError** if content is malformed
    """

    raise NotImplementedError('Unpacking not yet implemented for %s cells' % cls.NAME)

  def __eq__(self, other):
    return hash(self) == hash(other) if isinstance(other, Cell) else False

  def __ne__(self, other):
    return not self == other


class CircuitCell(Cell):
  """
  Cell concerning circuits.

  :var int circ_id: circuit id
  """

  def __init__(self, circ_id):
    self.circ_id = circ_id


class PaddingCell(Cell):
  """
  Randomized content to either keep activity going on a circuit.

  :var bytes payload: randomized payload
  """

  NAME = 'PADDING'
  VALUE = 0
  IS_FIXED_SIZE = True

  def __init__(self, payload = None):
    if not payload:
      payload = os.urandom(FIXED_PAYLOAD_LEN)
    elif len(payload) != FIXED_PAYLOAD_LEN:
      raise ValueError('Padding payload should be %i bytes, but was %i' % (FIXED_PAYLOAD_LEN, len(payload)))

    self.payload = payload

  def pack(self, link_version):
    return PaddingCell._pack(link_version, self.payload)

  @classmethod
  def _unpack(cls, content, circ_id, link_version):
    return PaddingCell(content)

  def __hash__(self):
    return _hash_attr(self, 'payload')


class CreateCell(CircuitCell):
  NAME = 'CREATE'
  VALUE = 1
  IS_FIXED_SIZE = True


class CreatedCell(CircuitCell):
  NAME = 'CREATED'
  VALUE = 2
  IS_FIXED_SIZE = True


class RelayCell(CircuitCell):
  """
  Command concerning a relay circuit.

  :var stem.client.RelayCommand command: reason the circuit is being closed
  :var int command_int: integer value of our command
  :var bytes data: payload of the cell
  :var int digest: running digest held with the relay
  :var int stream_id: specific stream this concerns
  """

  # TODO: Relay cells also have a 'recognized' field but from the spec I really
  # haven't a clue what the heck it is. The spec makes multiple mentions to
  # "when the 'recognized' field of a RELAY cell is zero" but no mention to if
  # it's non-zero or what the field actually is. :/
  #
  # For now just leaving it out. I'll file a ticket to ask about it later.

  NAME = 'RELAY'
  VALUE = 3
  IS_FIXED_SIZE = True

  def __init__(self, circ_id, command, data, digest, stream_id = 0):
    super(RelayCell, self).__init__(circ_id)
    self.command, self.command_int = stem.client.RelayCommand.get(command)
    self.data = data
    self.digest = digest
    self.stream_id = stream_id

    if not stream_id and self.command in stem.client.STREAM_ID_REQUIRED:
      raise ValueError('%s relay cells require a stream id' % self.command)
    elif stream_id and self.command in stem.client.STREAM_ID_DISALLOWED:
      raise ValueError('%s relay cells concern the circuit itself and cannot have a stream id' % self.command)

  def pack(self, link_version):
    payload = io.BytesIO()
    payload.write(Size.CHAR.pack(self.command_int))
    payload.write(Size.SHORT.pack(0))  # 'recognized' field
    payload.write(Size.SHORT.pack(self.stream_id))
    payload.write(Size.LONG.pack(self.digest))
    payload.write(Size.SHORT.pack(len(self.data)))
    payload.write(self.data)

    return RelayCell._pack(link_version, payload.getvalue(), self.circ_id)

  @classmethod
  def _unpack(cls, content, circ_id, link_version):
    command, content = Size.CHAR.pop(content)
    _, content = Size.SHORT.pop(content)  # 'recognized' field
    stream_id, content = Size.SHORT.pop(content)
    digest, content = Size.LONG.pop(content)
    data_len, content = Size.SHORT.pop(content)
    data, content = split(content, data_len)

    return RelayCell(circ_id, command, data, digest, stream_id)

  def __hash__(self):
    return _hash_attr(self, 'command_int', 'stream_id', 'digest', 'data')


class DestroyCell(CircuitCell):
  """
  Closes the given circuit.

  :var stem.client.CloseReason reason: reason the circuit is being closed
  :var int reason_int: integer value of our closure reason
  """

  NAME = 'DESTROY'
  VALUE = 4
  IS_FIXED_SIZE = True

  def __init__(self, circ_id, reason = stem.client.CloseReason.NONE):
    super(DestroyCell, self).__init__(circ_id)
    self.reason, self.reason_int = stem.client.CloseReason.get(reason)

  def pack(self, link_version):
    return DestroyCell._pack(link_version, Size.CHAR.pack(self.reason_int), self.circ_id)

  @classmethod
  def _unpack(cls, content, circ_id, link_version):
    content = content.rstrip(ZERO)

    if not content:
      content = ZERO
    elif len(content) > 1:
      raise ValueError('Circuit closure reason should be a single byte, but was %i' % len(content))

    return DestroyCell(circ_id, Size.CHAR.unpack(content))

  def __hash__(self):
    return _hash_attr(self, 'circ_id', 'reason_int')


class CreateFastCell(CircuitCell):
  """
  Create a circuit with our first hop. This is lighter weight than further hops
  because we've already established the relay's identity and secret key.

  :var bytes key_material: randomized key material
  """

  NAME = 'CREATE_FAST'
  VALUE = 5
  IS_FIXED_SIZE = True

  def __init__(self, circ_id, key_material = None):
    if not key_material:
      key_material = os.urandom(HASH_LEN)
    elif len(key_material) != HASH_LEN:
      raise ValueError('Key material should be %i bytes, but was %i' % (HASH_LEN, len(key_material)))

    super(CreateFastCell, self).__init__(circ_id)
    self.key_material = key_material

  def pack(self, link_version):
    return CreateFastCell._pack(link_version, self.key_material, self.circ_id)

  @classmethod
  def _unpack(cls, content, circ_id, link_version):
    content = content.rstrip(ZERO)

    if len(content) != HASH_LEN:
      raise ValueError('Key material should be %i bytes, but was %i' % (HASH_LEN, len(content)))

    return CreateFastCell(circ_id, content)

  def __hash__(self):
    return _hash_attr(self, 'circ_id', 'key_material')


class CreatedFastCell(CircuitCell):
  """
  CREATE_FAST reply.

  :var bytes key_material: randomized key material
  :var bytes derivative_key: hash proving the relay knows our shared key
  """

  NAME = 'CREATED_FAST'
  VALUE = 6
  IS_FIXED_SIZE = True

  def __init__(self, circ_id, derivative_key, key_material = None):
    if not key_material:
      key_material = os.urandom(HASH_LEN)
    elif len(key_material) != HASH_LEN:
      raise ValueError('Key material should be %i bytes, but was %i' % (HASH_LEN, len(key_material)))

    if len(derivative_key) != HASH_LEN:
      raise ValueError('Derivatived key should be %i bytes, but was %i' % (HASH_LEN, len(derivative_key)))

    super(CreatedFastCell, self).__init__(circ_id)
    self.key_material = key_material
    self.derivative_key = derivative_key

  def pack(self, link_version):
    return CreatedFastCell._pack(link_version, self.key_material + self.derivative_key, self.circ_id)

  @classmethod
  def _unpack(cls, content, circ_id, link_version):
    content = content.rstrip(ZERO)

    if len(content) != HASH_LEN * 2:
      raise ValueError('Key material and derivatived key should be %i bytes, but was %i' % (HASH_LEN * 2, len(content)))

    return CreatedFastCell(circ_id, content[HASH_LEN:], content[:HASH_LEN])

  def __hash__(self):
    return _hash_attr(self, 'circ_id', 'derivative_key', 'key_material')


class VersionsCell(Cell):
  """
  Link version negotiation cell.

  :var list versions: link versions
  """

  NAME = 'VERSIONS'
  VALUE = 7
  IS_FIXED_SIZE = False

  def __init__(self, versions):
    self.versions = versions

  def pack(self, link_version = None):
    # Used for link version negotiation so we don't have that yet. This is fine
    # since VERSION cells avoid most version dependent attributes.

    payload = b''.join([Size.SHORT.pack(v) for v in self.versions])
    return VersionsCell._pack(2, payload)

  @classmethod
  def _unpack(cls, content, circ_id, link_version):
    link_versions = []

    while content:
      version, content = Size.SHORT.pop(content)
      link_versions.append(version)

    return VersionsCell(link_versions)

  def __hash__(self):
    return _hash_attr(self, 'versions')


class NetinfoCell(Cell):
  """
  Information relays exchange about each other.

  :var datetime timestamp: current time
  :var stem.client.Address receiver_address: receiver's OR address
  :var list sender_addresses: sender's OR addresses
  """

  NAME = 'NETINFO'
  VALUE = 8
  IS_FIXED_SIZE = True

  def __init__(self, receiver_address, sender_addresses, timestamp = None):
    self.timestamp = timestamp if timestamp else datetime.datetime.now()
    self.receiver_address = receiver_address
    self.sender_addresses = sender_addresses

  def pack(self, link_version):
    payload = io.BytesIO()
    payload.write(Size.LONG.pack(int(datetime_to_unix(self.timestamp))))
    payload.write(self.receiver_address.pack())
    payload.write(Size.CHAR.pack(len(self.sender_addresses)))

    for addr in self.sender_addresses:
      payload.write(addr.pack())

    return NetinfoCell._pack(link_version, payload.getvalue())

  @classmethod
  def _unpack(cls, content, circ_id, link_version):
    if len(content) < 4:
      raise ValueError('NETINFO cell expected to start with a timestamp')

    timestamp, content = Size.LONG.pop(content)
    receiver_address, content = Address.pop(content)

    sender_addresses = []
    sender_addr_count, content = Size.CHAR.pop(content)

    for i in range(sender_addr_count):
      addr, content = Address.pop(content)
      sender_addresses.append(addr)

    return NetinfoCell(receiver_address, sender_addresses, datetime.datetime.utcfromtimestamp(timestamp))

  def __hash__(self):
    return _hash_attr(self, 'timestamp', 'receiver_address', 'sender_addresses')


class RelayEarlyCell(CircuitCell):
  NAME = 'RELAY_EARLY'
  VALUE = 9
  IS_FIXED_SIZE = True


class Create2Cell(CircuitCell):
  NAME = 'CREATE2'
  VALUE = 10
  IS_FIXED_SIZE = True


class Created2Cell(Cell):
  NAME = 'CREATED2'
  VALUE = 11
  IS_FIXED_SIZE = True


class PaddingNegotiateCell(Cell):
  NAME = 'PADDING_NEGOTIATE'
  VALUE = 12
  IS_FIXED_SIZE = True


class VPaddingCell(Cell):
  """
  Variable length randomized content to either keep activity going on a circuit.

  :var bytes payload: randomized payload
  """

  NAME = 'VPADDING'
  VALUE = 128
  IS_FIXED_SIZE = False

  def __init__(self, size = None, payload = None):
    if payload is None:
      payload = os.urandom(size) if size else os.urandom(random.randint(128, 1024))
    elif size is not None and size != len(payload):
      raise ValueError('VPaddingCell constructor specified both a size of %i bytes and payload of %i bytes' % (size, len(payload)))

    self.payload = payload

  def pack(self, link_version):
    return VPaddingCell._pack(link_version, self.payload)

  @classmethod
  def _unpack(cls, content, circ_id, link_version):
    return VPaddingCell(payload = content)

  def __hash__(self):
    return _hash_attr(self, 'payload')


class CertsCell(Cell):
  """
  Certificate held by the relay we're communicating with.

  :var list certificates: :class:`~stem.client.Certificate` of the relay
  """

  NAME = 'CERTS'
  VALUE = 129
  IS_FIXED_SIZE = False

  def __init__(self, certs):
    self.certificates = certs

  def pack(self, link_version):
    return CertsCell._pack(link_version, Size.CHAR.pack(len(self.certificates)) + ''.join([cert.pack() for cert in self.certificates]))

  @classmethod
  def _unpack(cls, content, circ_id, link_version):
    cert_count, content = Size.CHAR.pop(content)
    certs = []

    for i in range(cert_count):
      if not content:
        raise ValueError('CERTS cell indicates it should have %i certificates, but only contained %i' % (cert_count, len(certs)))

      cert, content = stem.client.Certificate.pop(content)
      certs.append(cert)

    return CertsCell(certs)

  def __hash__(self):
    return _hash_attr(self, 'certificates')


class AuthChallengeCell(Cell):
  """
  First step of the authentication handshake.

  :var bytes challenge: random bytes for us to sign to authenticate
  :var list methods: authentication methods supported by the relay we're
    communicating with
  """

  NAME = 'AUTH_CHALLENGE'
  VALUE = 130
  IS_FIXED_SIZE = False

  def __init__(self, methods, challenge = None):
    if not challenge:
      challenge = os.urandom(AUTH_CHALLENGE_SIZE)
    elif len(challenge) != AUTH_CHALLENGE_SIZE:
      raise ValueError('AUTH_CHALLENGE must be %i bytes, but was %i' % (AUTH_CHALLENGE_SIZE, len(challenge)))

    self.challenge = challenge
    self.methods = methods

  def pack(self, link_version):
    payload = io.BytesIO()
    payload.write(self.challenge)
    payload.write(Size.SHORT.pack(len(self.methods)))

    for method in self.methods:
      payload.write(Size.SHORT.pack(method))

    return AuthChallengeCell._pack(link_version, payload.getvalue())

  @classmethod
  def _unpack(cls, content, circ_id, link_version):
    if len(content) < AUTH_CHALLENGE_SIZE + 2:
      raise ValueError('AUTH_CHALLENGE payload should be at least 34 bytes, but was %i' % len(content))

    challenge, content = split(content, AUTH_CHALLENGE_SIZE)
    method_count, content = Size.SHORT.pop(content)

    if len(content) < method_count * 2:
      raise ValueError('AUTH_CHALLENGE should have %i methods, but only had %i bytes for it' % (method_count, len(content)))

    methods = []

    for i in range(method_count):
      method, content = Size.SHORT.pop(content)
      methods.append(method)

    return AuthChallengeCell(methods, challenge)

  def __hash__(self):
    return _hash_attr(self, 'challenge', 'methods')


class AuthenticateCell(Cell):
  NAME = 'AUTHENTICATE'
  VALUE = 131
  IS_FIXED_SIZE = False


class AuthorizeCell(Cell):
  NAME = 'AUTHORIZE'
  VALUE = 132
  IS_FIXED_SIZE = False
