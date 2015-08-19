# Copyright (c) 2015 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import json
import urllib
try:
    import xml.etree.cElementTree as ElementTree
except ImportError:
    import xml.etree.ElementTree as ElementTree

from swift.common.http import is_success
from swift.common.crypto_utils import CryptoWSGIContext
from swift.common.exceptions import EncryptionException
from swift.common.middleware.crypto import Crypto
from swift.common.request_helpers import strip_user_meta_prefix, is_user_meta,\
    get_obj_persisted_sysmeta_prefix, get_listing_content_type
from swift.common.swob import Request, HTTPException, HTTPInternalServerError
from swift.common.utils import get_logger, config_true_value, \
    parse_content_range, closing_if_possible


def _load_crypto_meta(value):
    """
    Build the crypto_meta from the json object.

    Note that json.loads always produces unicode strings, to ensure the
    resultant crypto_meta matches the original object cast all key and value
    data (other then the iv) to a str. This will work in py3 as well where all
    strings are unicode implying the cast is effectively a no-op.

    :param value: a string serialization of a crypto meta dict
    :returns: a dict containing crypto meta items
    """
    value = urllib.unquote_plus(value)
    try:
        crypto_meta = {str(name): (base64.b64decode(value)
                                   if name == 'iv' else str(value))
                       for name, value in json.loads(value).items()}
    except (ValueError, TypeError) as e:
        msg = "Could not decrypt. Bad crypto_meta: %r : %s" % (value, e)
        raise HTTPInternalServerError(body=msg, content_type='text/plain')

    if 'iv' not in crypto_meta or 'cipher' not in crypto_meta:
        msg = "Could not decrypt. Missing iv and/or cipher: %r" % value
        raise HTTPInternalServerError(body=msg, content_type='text/plain')
    return crypto_meta


class BaseDecrypterContext(CryptoWSGIContext):
    def __init__(self, decrypter, logger):
        super(BaseDecrypterContext, self).__init__(decrypter, logger)

    def _check_cipher(self, cipher):
        """
        Checks that a cipher is supported.
        :param cipher: name of cipher, a string
        :raises EncryptionException: if the cipher is not supported
        """
        if cipher != self.crypto.get_cipher():
            raise EncryptionException(
                "Encrypted with cipher %s, but can only decrypt with cipher %s"
                % (cipher, self.crypto.get_cipher()))

    def get_sysmeta_crypto_meta(self, header_name):
        """
        Extract a crypto_meta dict from a header.

        :param header_name: name of header that may have crypto_meta
        :return: A dict containing crypto_meta items
        """
        crypto_meta_json = self._response_header_value(header_name)

        if crypto_meta_json is None:
            return None

        return _load_crypto_meta(crypto_meta_json)

    def decrypt_value(self, value, key, crypto_meta):
        """
        Decrypt a value if suitable crypto_meta is provided or can be extracted
        from the value itself.

        :param value: value to decrypt
        :param key: crypto key to use
        :param crypto_meta: a crypto-meta dict of form returned by
            :py:func:`~swift.common.middleware.crypto.Crypto.get_crypto_meta`
        :returns: decrypted value if valid crypto_meta is found, otherwise the
            unmodified value
        :raises HTTPInternalServerError: if the crypto_meta cipher is not
            supported
        """
        if not value:
            return ''

        if crypto_meta is None:
            # try to extract crypto_meta from end of value
            parts = value.rsplit(';', 1)
            if len(parts) == 2:
                value, param = parts
                if param.strip().startswith('meta='):
                    param = param.strip()[5:]
                    try:
                        crypto_meta = _load_crypto_meta(param)
                    except (TypeError, ValueError):
                        pass
        if crypto_meta is None:
            # it's not an error to have been passed an unencrypted value
            return value
        try:
            self._check_cipher(crypto_meta.get('cipher'))
        except EncryptionException as err:
            msg = 'Error decrypting header value'
            self.logger.error('%s: %s' % (msg, str(err)))
            raise HTTPInternalServerError(body=msg, content_type='text/plain')

        crypto_ctxt = self.crypto.create_decryption_ctxt(
            key, crypto_meta['iv'], 0)
        return crypto_ctxt.update(base64.b64decode(value))

    def process_resp(self, req):
        """
        Determine if a response should be decrypted, and if so then fetch keys.

        :param req: a Request object
        :returns: a dict if decryption keys
        """
        # Only proceed processing if an error has not occurred
        if not is_success(self._get_status_int()):
            return None

        if config_true_value(req.environ.get('swift.crypto.override')):
            self.logger.debug('No decryption is necessary because of override')
            return None

        return self.get_keys(req.environ)


class DecrypterObjContext(BaseDecrypterContext):
    def __init__(self, decrypter, logger):
        super(DecrypterObjContext, self).__init__(decrypter, logger)
        self.server_type = 'object'
        self.body_crypto_ctxt = None

    def decrypt_user_metadata(self, keys):
        prefix = "%scrypto-meta-" % get_obj_persisted_sysmeta_prefix()
        result = []
        for name, val in self._response_headers:
            if is_user_meta(self.server_type, name) and val:
                short_name = strip_user_meta_prefix(self.server_type, name)
                crypto_meta = self.get_sysmeta_crypto_meta(prefix + short_name)
                if not crypto_meta:
                    # This is not an error - some user meta headers may not
                    # be encrypted
                    self.logger.debug("No crypto meta for user metadata %s"
                                      % name)
                    continue
                # the corresponding value must have been encrypted/encoded
                value = self.decrypt_value(
                    val, keys[self.server_type], crypto_meta)
                result.append((name, value))

                self.logger.debug("decrypted user metadata %s = %s"
                                  % (name, value))
        return result

    def decrypt_resp_headers(self, keys):
        """
        Find encrypted headers and replace with the decrypted versions.

        :param keys: a dict of decryption keys.
        :return: A list of headers with any encrypted headers replaced by their
                 decrypted values.
        """
        mod_hdr_pairs = []

        # Decrypt plaintext etag and place in Etag header for client response
        # TODO: check this header exists at same time as checking crypto-meta
        etag = self._response_header_value('X-Object-Sysmeta-Crypto-Etag')
        crypto_meta = self.get_sysmeta_crypto_meta(
            'X-Object-Sysmeta-Crypto-Meta-Etag')
        if crypto_meta:
            mod_hdr_pairs.append(('Etag', self.decrypt_value(
                etag, keys[self.server_type], crypto_meta)))

        # Decrypt content-type
        ctype = self._response_header_value('Content-Type')
        mod_hdr_pairs.append(('Content-Type', self.decrypt_value(
            ctype, keys[self.server_type], None)))

        # Decrypt all user metadata
        mod_hdr_pairs.extend(self.decrypt_user_metadata(keys))

        mod_hdr_names = map(lambda h: h[0].lower(), mod_hdr_pairs)
        mod_resp_headers = filter(lambda h: h[0].lower() not in mod_hdr_names,
                                  self._response_headers)

        for pair in mod_hdr_pairs:
            mod_resp_headers.append(pair)

        return mod_resp_headers

    def make_decryption_context(self, keys):
        body_crypto_meta = self.get_sysmeta_crypto_meta(
            'X-Object-Sysmeta-Crypto-Meta')

        if not body_crypto_meta:
            # TODO should this be an error i.e. should we never expect to get
            # if keymaster is behaving correctly and sets crypto.override flag?
            self.logger.warn("Warning: No sysmeta-crypto-meta for body.")
            return None

        try:
            self._check_cipher(body_crypto_meta.get('cipher'))
        except EncryptionException as err:
            msg = 'Error creating decryption context for object body'
            self.logger.error('%s: %s' % (msg, str(err)))
            raise HTTPInternalServerError(body=msg, content_type='text/plain')

        content_range = self._response_header_value('Content-Range')
        offset = 0
        if content_range:
            # Determine the offset within the whole object if ranged GET
            offset, end, total = parse_content_range(content_range)
            self.logger.debug("Range is: %s - %s, %s" % (offset, end, total))

        return self.crypto.create_decryption_ctxt(
            keys['object'], body_crypto_meta.get('iv'), offset)

    def GET(self, req, start_response):
        app_resp = self._app_call(req.environ)

        keys = self.process_resp(req)

        if keys is None:
            # skip decryption
            start_response(self._response_status, self._response_headers,
                           self._response_exc_info)
            return app_resp

        self.body_crypto_ctxt = self.make_decryption_context(keys)
        mod_resp_headers = self.decrypt_resp_headers(keys)

        start_response(self._response_status, mod_resp_headers,
                       self._response_exc_info)

        if self.body_crypto_ctxt is None:
            return app_resp

        def iter_response(iterable, crypto_ctxt):
            with closing_if_possible(iterable):
                for chunk in iterable:
                    yield crypto_ctxt.update(chunk)

        return iter_response(app_resp, self.body_crypto_ctxt)

    def HEAD(self, req, start_response):
        app_resp = self._app_call(req.environ)

        keys = self.process_resp(req)

        if keys is None:
            # skip decryption
            start_response(self._response_status, self._response_headers,
                           self._response_exc_info)
        else:
            mod_resp_headers = self.decrypt_resp_headers(keys)
            start_response(self._response_status, mod_resp_headers,
                           self._response_exc_info)

        return app_resp


class DecrypterContContext(BaseDecrypterContext):
    def __init__(self, decrypter, logger):
        super(DecrypterContContext, self).__init__(decrypter, logger)

    def GET(self, req, start_response):
        app_resp = self._app_call(req.environ)

        keys = self.process_resp(req)

        if keys:
            out_content_type = get_listing_content_type(req)
            if out_content_type == 'application/json':
                app_resp = self.process_json_resp(keys, app_resp)
            elif out_content_type.endswith('/xml'):
                app_resp = self.process_xml_resp(keys, app_resp)

        start_response(self._response_status,
                       self._response_headers,
                       self._response_exc_info)

        return app_resp

    def update_content_length(self, new_total_len):
        self._response_headers = [
            (h, v) for h, v in self._response_headers
            if h.lower() != 'content-length']
        self._response_headers.append(('Content-Length', str(new_total_len)))

    def process_json_resp(self, keys, resp_iter):
        """
        Parses json body listing and decrypt content-type entries. Updates
        Content-Length header with new body length and return a body iter.
        """
        with closing_if_possible(resp_iter):
            resp_body = ''.join(resp_iter)
        body_json = json.loads(resp_body)
        new_body = json.dumps([self.decrypt_obj_dict(obj_dict, keys)
                               for obj_dict in body_json])
        self.update_content_length(len(new_body))
        return [new_body]

    def decrypt_obj_dict(self, obj_dict, keys):
        ciphertext = obj_dict['content_type']
        obj_dict['content_type'] = self.decrypt_value(
            ciphertext, keys['container'], None)

        # TODO - decode/decrypt etag when not using FakeFooters
        # if etag and (len(etag) > constraints.ETAG_LENGTH):
        return obj_dict

    def process_xml_resp(self, keys, resp_iter):
        """
        Parses xml body listing and decrypt content-type entries. Updates
        Content-Length header with new body length and return a body iter.
        """
        with closing_if_possible(resp_iter):
            resp_body = ''.join(resp_iter)
        tree = ElementTree.fromstring(resp_body)
        for elem in tree.iter('content_type'):
            ciphertext = elem.text.encode('utf8')
            plaintext = self.decrypt_value(ciphertext, keys['container'], None)
            elem.text = plaintext.decode('utf8')
        new_body = ElementTree.tostring(tree, encoding='UTF-8').replace(
            "<?xml version='1.0' encoding='UTF-8'?>",
            '<?xml version="1.0" encoding="UTF-8"?>', 1)
        self.update_content_length(len(new_body))
        return [new_body]


class Decrypter(object):

    def __init__(self, app, conf):
        self.app = app
        self.logger = get_logger(conf, log_route="decrypter")
        self.conf = conf

    def __call__(self, env, start_response):
        self.crypto = get_crypto(self.conf)

        req = Request(env)
        try:
            parts = req.split_path(3, 4, True)
        except ValueError:
            return self.app(env, start_response)

        if parts[3] and hasattr(DecrypterObjContext, req.method):
            dec_context = DecrypterObjContext(self, self.logger)
        elif parts[2] and hasattr(DecrypterContContext, req.method):
            dec_context = DecrypterContContext(self, self.logger)
        else:
            # url and/or request verb is not handled by decrypter
            dec_context = None

        if dec_context:
            try:
                return getattr(dec_context, req.method)(req, start_response)
            except HTTPException as err_resp:
                return err_resp(env, start_response)

        return self.app(env, start_response)


def get_crypto(conf):
    return Crypto(conf)


def filter_factory(global_conf, **local_conf):
    conf = global_conf.copy()
    conf.update(local_conf)

    def decrypter_filter(app):
        return Decrypter(app, conf)
    return decrypter_filter
