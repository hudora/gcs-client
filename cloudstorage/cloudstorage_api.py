# Copyright 2012 Google Inc. All Rights Reserved.

"""File Interface for Google Cloud Storage."""



from __future__ import with_statement



__all__ = ['delete',
           'listbucket',
           'open',
           'stat',
          ]

import os
import urllib
import xml.etree.ElementTree as ET
from . import common
from . import errors
from . import storage_api


def open(filename,
         mode='r',
         content_type=None,
         options=None,
         read_buffer_size=storage_api.ReadBuffer.DEFAULT_BUFFER_SIZE):
  """Opens a Google Cloud Storage file and returns it as a File-like object.

  Args:
    filename: a cloud storage filename of form '/bucket/filename'.
    mode: 'r' for reading mode. 'w' for writing mode.
      In reading mode, the file must exist. In writing mode, a file will
      be created or be overrode.
    content_type: the MIME type of the file. str. Only valid in writing mode.
    options: a str->basestring dict to specify additional cloud storage
      options. e.g. {'x-goog-acl': 'private', 'x-goog-meta-foo': 'foo'}
      Currently supported options are x-goog-acl and x-goog-meta-.
      Only valid in writing mode.
      See https://developers.google.com/storage/docs/reference-headers
      for details.
    read_buffer_size: the buffer size for read. If buffer is empty, the read
      stream will asynchronously prefetch a new buffer before the next read().
      To minimize blocking for large files, always read in buffer size.
      To minimize number of requests for small files, set a larger
      buffer size.

  Returns:
    A reading or writing buffer that supports File-like interface. Buffer
    must be closed after operations are done.

  Raises:
    errors.AuthorizationError: if authorization failed.
    errors.NotFoundError: if an object that's expected to exist doesn't.
    ValueError: invalid open mode or if content_type or options are specified
      in reading mode.
  """
  common.validate_file_path(filename)
  api = _get_storage_api()

  if mode == 'w':
    common.validate_options(options)
    return storage_api.StreamingBuffer(api, filename, content_type, options)
  elif mode == 'r':
    if content_type or options:
      raise ValueError('Options and content_type can only be specified '
                       'for writing mode.')
    return storage_api.ReadBuffer(api,
                                  filename,
                                  max_buffer_size=read_buffer_size)
  else:
    raise ValueError('Invalid mode %s.' % mode)


def delete(filename):
  """Delete a cloud storage file.

  Args:
    filename: a cloud storage filename of form '/bucket/filename'.

  Raises:
    errors.NotFoundError: if the file doesn't exist prior to deletion.
  """
  api = _get_storage_api()
  common.validate_file_path(filename)
  status, _, _ = api.delete_object(filename)
  errors.check_status(status, [204])


def stat(filename):
  """Get CSFileStat of a cloud storage file.

  Args:
    filename: a cloud storage filename of form '/bucket/filename'.

  Returns:
    a CSFileStat object containing info about this file.

  Raises:
    errors.AuthorizationError: if authorization failed.
    errors.NotFoundError: if an object that's expected to exist doesn't.
  """
  common.validate_file_path(filename)
  api = _get_storage_api()
  status, headers, _ = api.head_object(filename)
  errors.check_status(status, [200])
  file_stat = common.CSFileStat(
      filename=filename,
      st_size=long(headers.get('content-length')),
      st_ctime=common.http_time_to_posix(headers.get('last-modified')),
      etag=headers.get('etag'),
      content_type=headers.get('content-type'),
      metadata=common.get_metadata(headers))

  return file_stat


def listbucket(bucket, marker=None, prefix=None, max_keys=None):
  """Return an CSFileStat iterator over files in the given bucket.

  Optional arguments are to limit the result to a subset of files under bucket.

  This function is asynchronous. It does not block unless iterator is called
  before the iterator gets result.

  Args:
    bucket: a cloud storage bucket of form "/bucket".
    marker: a string after which (exclusive) to start listing.
    prefix: limits the returned filenames to those with this prefix. no regex.
    max_keys: the maximum number of filenames to match. int.

  Example:
    For files "/bucket/foo1", "/bucket/foo2", "/bucket/foo3", "/bucket/www",
    listbucket("/bucket", prefix="foo", marker="foo1")
    will match "/bucket/foo2" and "/bucket/foo3".

    See Google Cloud Storage documentation for more details and examples.
    https://developers.google.com/storage/docs/reference-methods#getbucket

  Returns:
    An GSFileStat iterator over matched files, sorted by filename.
    Only filename, etag, and st_size are set in these GSFileStat objects.
  """
  common.validate_bucket_path(bucket)
  api = _get_storage_api()
  options = {}
  if marker:
    options['marker'] = marker
  if max_keys:
    options['max-keys'] = max_keys
  if prefix:
    options['prefix'] = prefix

  return _Bucket(api, bucket, options)


class _Bucket(object):
  """A wrapper for a GCS bucket as the return value of listbucket."""

  def __init__(self, api, path, options):
    """Initialize.

    Args:
      api: storage_api instance.
      path: bucket path of form '/bucket'.
      options: a dict of listbucket options. Please see listbucket doc.
    """
    self._api = api
    self._path = path
    self._options = options.copy()
    self._get_bucket_fut = self._api.get_bucket_async(
        self._path + '?' + urllib.urlencode(self._options))

  def _add_ns(self, tagname):
    return '{%(ns)s}%(tag)s' % {'ns': common.CS_XML_NS,
                                'tag': tagname}

  def __iter__(self):
    """Iter over the bucket.

    Yields:
      CSFileStat: a CSFileStat for an object in the bucket.
        They are ordered by CSFileStat.filename.
    """
    total = 0
    while self._get_bucket_fut:
      status, _, content = self._get_bucket_fut.get_result()
      errors.check_status(status, [200])
      root = ET.fromstring(content)
      for contents in root.getiterator(self._add_ns('Contents')):
        yield common.CSFileStat(
            self._path + '/' + contents.find(self._add_ns('Key')).text,
            long(contents.find(self._add_ns('Size')).text),
            contents.find(self._add_ns('ETag')).text)
        total += 1

      max_keys = root.find(self._add_ns('MaxKeys'))
      next_marker = root.find(self._add_ns('NextMarker'))
      if (max_keys is None or total < int(max_keys.text)) and (
          next_marker is not None):
        self._options['marker'] = next_marker.text
        self._get_bucket_fut = self._api.get_bucket_async(
            self._path + '?' + urllib.urlencode(self._options))
      else:
        self._get_bucket_fut = None


def _get_storage_api():
  """Returns storage_api instance for API methods.

  Returns:
    A storage_api instance to handle urlfetch work to GCS.
    On dev appserver, this instance by default will talk to a local stub
    unless common.ACCESS_TOKEN is set. That token will be used to talk
    to the real GCS.
  """
  api = storage_api._StorageApi(storage_api._StorageApi.full_control_scope)
  if not os.environ.get('DATACENTER') and not common.get_access_token():
    api.api_url = common.LOCAL_API_URL
  if common.get_access_token():
    api.token = common.get_access_token()
  return api
