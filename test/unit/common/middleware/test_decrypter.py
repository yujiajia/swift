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

import unittest
from xml.dom import minidom
import mock
import base64
import json
import urllib

from swift.common.middleware import decrypter
from swift.common.swob import Request, HTTPException, HTTPOk, \
    HTTPInternalServerError
from test.unit.common.middleware.crypto_helpers import FakeCrypto, md5hex, \
    fake_encrypt, fetch_crypto_keys
from test.unit.common.middleware.helpers import FakeSwift, FakeAppThatExcepts


class FakeContextThrows(object):

    @staticmethod
    def get_error_msg():
        return 'Testing context update exception'

    def update(self, chunk):
        raise HTTPInternalServerError(self.get_error_msg())


class FakeCryptoThrows(FakeCrypto):

    def create_encryption_ctxt(self, key, iv):
        return FakeCrypto()

    def create_decryption_ctxt(self, key, iv, offset):
        return FakeContextThrows()


def get_crypto_meta():
    fc = FakeCrypto()
    return {'iv': 'someIV', 'cipher': fc.get_cipher()}


def get_crypto_meta_header(crypto_meta=None):
    if crypto_meta is None:
        crypto_meta = get_crypto_meta()
    return urllib.quote_plus(
        json.dumps({key: (base64.b64encode(value).decode()
                          if key == 'iv' else value)
                    for key, value in crypto_meta.items()}))


def get_content_type():
    return 'text/plain'


def encrypt_and_append_meta(value, crypto_meta=None):
    return '%s; meta=%s' % (
        base64.b64encode(fake_encrypt(value)),
        get_crypto_meta_header(crypto_meta))


@mock.patch('swift.common.middleware.decrypter.Crypto', FakeCrypto)
class TestDecrypterObjectRequests(unittest.TestCase):

    def test_basic_get_req(self):
        env = {'REQUEST_METHOD': 'GET',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        enc_body = fake_encrypt(body)
        content_type = encrypt_and_append_meta('text/plain')
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'content-type': content_type,
                'content-length': len(enc_body),
                'X-Object-Sysmeta-Crypto-Etag':
                    base64.b64encode(fake_encrypt(md5hex(body))),
                'X-Object-Sysmeta-Crypto-Meta-Etag': get_crypto_meta_header(),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header(),
                'x-object-meta-test':
                    base64.b64encode(fake_encrypt('encrypt me')),
                'x-object-sysmeta-crypto-meta-test': get_crypto_meta_header(),
                'x-object-sysmeta-test': 'do not encrypt me'}
        app.register('GET', '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.body, body)
        self.assertEqual(resp.status, '200 OK')
        self.assertEqual(resp.headers['Etag'], md5hex(body))
        self.assertEqual(resp.headers['Content-Type'], 'text/plain')
        self.assertEqual(resp.headers['x-object-meta-test'], 'encrypt me')
        self.assertEqual(resp.headers['x-object-sysmeta-test'],
                         'do not encrypt me')

    def test_get_req_body_decrypt_throws(self):
        # simulate headers not being encrypted so that first call to decrypter
        # context will be when body is read
        env = {'REQUEST_METHOD': 'GET',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        enc_body = fake_encrypt(body)
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'content-type': 'text/plain',
                'content-length': len(enc_body),
                'X-Object-Sysmeta-Crypto-Etag': md5hex(body),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header()}
        app.register('GET', '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        with mock.patch('swift.common.middleware.decrypter.Crypto',
                        FakeCryptoThrows):
            resp = req.get_response(decrypter.Decrypter(app, {}))
            with self.assertRaises(HTTPException) as catcher:
                resp.body
        self.assertEqual(catcher.exception.body,
                         FakeContextThrows.get_error_msg())

    def _test_req_hdr_decrypt_throws(self, method):
        # make headers encrypted so that decrypter context will blow up during
        # header processing
        env = {'REQUEST_METHOD': method,
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        enc_body = fake_encrypt(body)
        content_type = encrypt_and_append_meta('text/plain')
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'content-type': content_type,
                'content-length': len(enc_body),
                'X-Object-Sysmeta-Crypto-Etag': md5hex(body),
                'X-Object-Sysmeta-Crypto-Meta-Etag': get_crypto_meta_header(),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header()}
        app.register(method, '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        with mock.patch('swift.common.middleware.decrypter.Crypto',
                        FakeCryptoThrows):
            resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.status, '500 Internal Error')
        return resp

    def test_head_req_hdr_decrypt_throws(self):
        self._test_req_hdr_decrypt_throws('HEAD')

    def test_get_req_hdr_decrypt_throws(self):
        resp = self._test_req_hdr_decrypt_throws('GET')
        self.assertEqual(FakeContextThrows.get_error_msg(), resp.body)

    def test_basic_head_req(self):
        env = {'REQUEST_METHOD': 'HEAD',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        enc_body = fake_encrypt(body)
        content_type = encrypt_and_append_meta('text/plain')
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'etag': 'hashOfCiphertext',
                'content-type': content_type,
                'content-length': len(enc_body),
                'X-Object-Sysmeta-Crypto-Etag':
                    base64.b64encode(fake_encrypt(md5hex(body))),
                'X-Object-Sysmeta-Crypto-Meta-Etag': get_crypto_meta_header(),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header(),
                'x-object-meta-test':
                    base64.b64encode(fake_encrypt('encrypt me')),
                'x-object-sysmeta-crypto-meta-test': get_crypto_meta_header(),
                'x-object-sysmeta-test': 'do not encrypt me'}
        app.register('HEAD', '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.status, '200 OK')
        self.assertEqual(resp.headers['Etag'], md5hex(body))
        self.assertEqual(resp.headers['Content-Type'], 'text/plain')
        self.assertEqual(resp.headers['x-object-meta-test'], 'encrypt me')
        self.assertEqual(resp.headers['x-object-sysmeta-test'],
                         'do not encrypt me')

    def _test_req_content_type_not_encrypted(self, method):
        # check that content_type is not decrypted if it does not have crypto
        # meta (testing for future cases where content_type may be updated
        # as part of an unencrypted POST).
        env = {'REQUEST_METHOD': method,
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        enc_body = fake_encrypt(body)
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'etag': 'hashOfCiphertext',
                'content-type': 'text/plain',
                'content-length': len(enc_body),
                'X-Object-Sysmeta-Crypto-Etag':
                    base64.b64encode(fake_encrypt(md5hex(body))),
                'X-Object-Sysmeta-Crypto-Meta-Etag': get_crypto_meta_header(),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header()}
        app.register(method, '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.status, '200 OK')
        self.assertEqual(resp.headers['Etag'], md5hex(body))
        self.assertEqual(resp.headers['Content-Type'], 'text/plain')

    def test_head_req_content_type_not_encrypted(self):
        self._test_req_content_type_not_encrypted('HEAD')

    def test_get_req_content_type_not_encrypted(self):
        self._test_req_content_type_not_encrypted('GET')

    def _test_req_metadata_not_encrypted(self, method):
        # check that metadata is not decrypted if it does not have crypto meta;
        # testing for case of an unencrypted POST to an object.
        env = {'REQUEST_METHOD': method,
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        enc_body = fake_encrypt(body)
        content_type = encrypt_and_append_meta('text/plain')
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'etag': 'hashOfCiphertext',
                'content-type': content_type,
                'content-length': len(enc_body),
                'X-Object-Sysmeta-Crypto-Etag':
                    base64.b64encode(fake_encrypt(md5hex(body))),
                'X-Object-Sysmeta-Crypto-Meta-Etag': get_crypto_meta_header(),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header(),
                'x-object-meta-test': 'plaintext'}
        app.register(method, '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.status, '200 OK')
        self.assertEqual(resp.headers['Etag'], md5hex(body))
        self.assertEqual(resp.headers['Content-Type'], 'text/plain')
        self.assertEqual(resp.headers['x-object-meta-test'], 'plaintext')

    def test_head_req_metadata_not_encrypted(self):
        self._test_req_metadata_not_encrypted('HEAD')

    def test_get_req_metadata_not_encrypted(self):
        self._test_req_metadata_not_encrypted('GET')

    def test_get_req_unencrypted_data(self):
        # testing case of an unencrypted object with encrypted metadata from
        # a later POST
        env = {'REQUEST_METHOD': 'GET',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        app = FakeSwift()
        hdrs = {'Etag': md5hex(body),
                'content-type': 'text/plain',
                'content-length': len(body),
                'x-object-meta-test':
                    base64.b64encode(fake_encrypt('encrypt me')),
                'x-object-sysmeta-crypto-meta-test': get_crypto_meta_header(),
                'x-object-sysmeta-test': 'do not encrypt me'}
        app.register('GET', '/v1/a/c/o', HTTPOk, body=body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.body, body)
        self.assertEqual(resp.status, '200 OK')
        self.assertEqual(resp.headers['Etag'], md5hex(body))
        self.assertEqual(resp.headers['Content-Type'], 'text/plain')
        # POSTed user meta was encrypted
        self.assertEqual(resp.headers['x-object-meta-test'], 'encrypt me')
        # PUT sysmeta was not encrypted
        self.assertEqual(resp.headers['x-object-sysmeta-test'],
                         'do not encrypt me')

    def test_multiseg_get_obj(self):
        env = {'REQUEST_METHOD': 'GET',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        chunks = ['some', 'chunks', 'of data']
        body = ''.join(chunks)
        enc_body = [fake_encrypt(chunk) for chunk in chunks]
        content_type = encrypt_and_append_meta('text/plain')
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'content-type': content_type,
                'content-length': sum(map(len, enc_body)),
                'X-Object-Sysmeta-Crypto-Etag':
                    base64.b64encode(fake_encrypt(md5hex(body))),
                'X-Object-Sysmeta-Crypto-Meta-Etag': get_crypto_meta_header(),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header()}
        app.register('GET', '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.body, body)
        self.assertEqual(resp.status, '200 OK')
        self.assertEqual(resp.headers['Etag'], md5hex(body))
        self.assertEqual(resp.headers['Content-Type'], 'text/plain')

    def test_multiseg_get_range_obj(self):
        env = {'REQUEST_METHOD': 'GET',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        req.headers['Content-Range'] = 'bytes 3-10/17'
        chunks = ['0123', '45678', '9abcdef']
        body = ''.join(chunks)
        enc_body = [fake_encrypt(chunk) for chunk in chunks]
        enc_body = [enc_body[0][3:], enc_body[1], enc_body[2][:2]]
        content_type = encrypt_and_append_meta('text/plain')
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'content-type': content_type,
                'content-length': sum(map(len, enc_body)),
                'X-Object-Sysmeta-Crypto-Etag':
                    base64.b64encode(fake_encrypt(md5hex(body))),
                'X-Object-Sysmeta-Crypto-Meta-Etag': get_crypto_meta_header(),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header()}
        app.register('GET', '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.body, '3456789a')
        self.assertEqual(resp.status, '200 OK')
        # TODO - how do we validate the range body if etag is for whole? Is
        # the test actually faking the correct Etag in response?
        self.assertEqual(resp.headers['Etag'], md5hex(body))
        self.assertEqual(resp.headers['Content-Type'], 'text/plain')

    def test_etag_no_match_on_get(self):
        self.skipTest('Etag verification not yet implemented')
        env = {'REQUEST_METHOD': 'GET',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        enc_body = fake_encrypt(body)
        content_type = encrypt_and_append_meta('text/plain')
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'content-type': content_type,
                'content-length': len(enc_body),
                'X-Object-Sysmeta-Crypto-Etag': md5hex('not the body'),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header()}
        app.register('GET', '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.status, '500 Internal Error')

    def test_missing_key_callback(self):
        # Do not provide keys, and do not set override flag
        env = {'REQUEST_METHOD': 'GET'}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        enc_body = fake_encrypt(body)
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'content-type': 'text/plain',
                'content-length': len(enc_body),
                'X-Object-Sysmeta-Crypto-Etag': md5hex('not the body'),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header()}
        app.register('GET', '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.status, '500 Internal Error')
        self.assertEqual(
            resp.body, 'swift.crypto.fetch_crypto_keys not in env')

    def test_error_in_key_callback(self):
        def raise_exc():
            raise Exception('Testing')

        env = {'REQUEST_METHOD': 'GET',
               'swift.crypto.fetch_crypto_keys': raise_exc}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        enc_body = fake_encrypt(body)
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'content-type': 'text/plain',
                'content-length': len(enc_body),
                'X-Object-Sysmeta-Crypto-Etag': md5hex(body),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header()}
        app.register('GET', '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.status, '500 Internal Error')
        self.assertEqual(
            resp.body, 'swift.crypto.fetch_crypto_keys had exception: Testing')

    def test_cipher_mismatch_for_body(self):
        # Cipher does not match
        env = {'REQUEST_METHOD': 'GET',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        enc_body = fake_encrypt(body)
        app = FakeSwift()
        bad_crypto_meta = get_crypto_meta()
        bad_crypto_meta['cipher'] = 'unknown_cipher'
        hdrs = {'Etag': 'hashOfCiphertext',
                'content-type': 'text/plain',
                'content-length': len(enc_body),
                'X-Object-Sysmeta-Crypto-Etag': md5hex(body),
                'X-Object-Sysmeta-Crypto-Meta':
                    get_crypto_meta_header(crypto_meta=bad_crypto_meta)}
        app.register('GET', '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.status, '500 Internal Error')
        self.assertEqual('Error creating decryption context for object body',
                         resp.body)

    def test_cipher_mismatch_for_content_type(self):
        # Cipher does not match
        env = {'REQUEST_METHOD': 'GET',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        enc_body = fake_encrypt(body)
        bad_crypto_meta = get_crypto_meta()
        bad_crypto_meta['cipher'] = 'unknown_cipher'
        content_type = encrypt_and_append_meta('text/plain',
                                               crypto_meta=bad_crypto_meta)
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'content-type': content_type,
                'content-length': len(enc_body),
                'X-Object-Sysmeta-Crypto-Etag': md5hex(body),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header()}
        app.register('GET', '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.status, '500 Internal Error')
        self.assertEqual('Error decrypting header value', resp.body)

    def test_cipher_mismatch_for_metadata(self):
        # Cipher does not match
        env = {'REQUEST_METHOD': 'GET',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        enc_body = fake_encrypt(body)
        bad_crypto_meta = get_crypto_meta()
        bad_crypto_meta['cipher'] = 'unknown_cipher'
        content_type = encrypt_and_append_meta('text/plain')
        app = FakeSwift()
        hdrs = {'Etag': 'hashOfCiphertext',
                'content-type': content_type,
                'content-length': len(enc_body),
                'X-Object-Sysmeta-Crypto-Etag': md5hex(body),
                'X-Object-Sysmeta-Crypto-Meta': get_crypto_meta_header(),
                'x-object-meta-test':
                    base64.b64encode(fake_encrypt('encrypt me')),
                'x-object-sysmeta-crypto-meta-test':
                    get_crypto_meta_header(crypto_meta=bad_crypto_meta)}
        app.register('GET', '/v1/a/c/o', HTTPOk, body=enc_body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.status, '500 Internal Error')
        self.assertEqual('Error decrypting header value', resp.body)

    def test_decryption_override(self):
        # This covers the case of an old un-encrypted object
        env = {'REQUEST_METHOD': 'GET',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys,
               'swift.crypto.override': True}
        req = Request.blank('/v1/a/c/o', environ=env)
        body = 'FAKE APP'
        app = FakeSwift()
        hdrs = {'Etag': md5hex(body),
                'content-type': 'text/plain',
                'content-length': len(body),
                'x-object-meta-test': 'do not encrypt me',
                'x-object-sysmeta-test': 'do not encrypt me'}
        app.register('GET', '/v1/a/c/o', HTTPOk, body=body, headers=hdrs)
        resp = req.get_response(decrypter.Decrypter(app, {}))
        self.assertEqual(resp.body, body)
        self.assertEqual(resp.status, '200 OK')
        self.assertEqual(resp.headers['Etag'], md5hex(body))
        self.assertEqual(resp.headers['Content-Type'], 'text/plain')
        self.assertEqual(resp.headers['x-object-meta-test'],
                         'do not encrypt me')
        self.assertEqual(resp.headers['x-object-sysmeta-test'],
                         'do not encrypt me')


@mock.patch('swift.common.middleware.decrypter.Crypto', FakeCrypto)
class TestDecrypterContainerRequests(unittest.TestCase):
    # TODO - update these tests to have etag to be encrypted and have
    # crypto-meta in response, and verify that the etag gets decrypted.
    def _make_cont_get_req(self, resp_body, format, override=False):
        path = '/v1/a/c'
        content_type = 'text/plain'
        if format:
            path = '%s/?format=%s' % (path, format)
            content_type = 'application/' + format
        env = {'REQUEST_METHOD': 'GET',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        if override:
            env['swift.crypto.override'] = True
        req = Request.blank(path, environ=env)
        app = FakeSwift()
        hdrs = {'content-type': content_type}
        app.register('GET', path, HTTPOk, body=resp_body, headers=hdrs)
        return req.get_response(decrypter.Decrypter(app, {}))

    def test_cont_get_simple_req(self):
        # no format requested, listing has names only
        fake_body = 'testfile1\ntestfile2\n'

        resp = self._make_cont_get_req(fake_body, None)

        self.assertEqual('200 OK', resp.status)
        names = resp.body.split('\n')
        self.assertEqual(3, len(names))
        self.assertIn('testfile1', names)
        self.assertIn('testfile2', names)
        self.assertIn('', names)

    def test_cont_get_json_req(self):
        content_type_1 = u'\uF10F\uD20D\uB30B\u9409'
        content_type_2 = 'text/plain; param=foo'

        obj_dict_1 = {"bytes": 16,
                      "last_modified": "2015-04-14T23:33:06.439040",
                      "hash": "c6e8196d7f0fff6444b90861fe8d609d",
                      "name": "testfile",
                      "content_type":
                      encrypt_and_append_meta(content_type_1.encode('utf8'))}

        obj_dict_2 = {"bytes": 24,
                      "last_modified": "2015-04-14T23:33:06.519020",
                      "hash": "ac0374ed4d43635f803c82469d0b5a10",
                      "name": "testfile2",
                      "content_type":
                      encrypt_and_append_meta(content_type_2.encode('utf8'))}

        listing = [obj_dict_1, obj_dict_2]
        fake_body = json.dumps(listing)

        resp = self._make_cont_get_req(fake_body, 'json')

        self.assertEqual('200 OK', resp.status)
        body = resp.body
        self.assertEqual(len(body), int(resp.headers['Content-Length']))
        body_json = json.loads(body)
        self.assertEqual(2, len(body_json))
        obj_dict_1['content_type'] = content_type_1
        self.assertDictEqual(obj_dict_1, body_json[0])
        obj_dict_2['content_type'] = content_type_2
        self.assertDictEqual(obj_dict_2, body_json[1])

    def test_cont_get_json_req_with_crypto_override(self):
        content_type_1 = 'image/jpeg'
        content_type_2 = 'text/plain; param=foo'

        obj_dict_1 = {"bytes": 16,
                      "last_modified": "2015-04-14T23:33:06.439040",
                      "hash": "c6e8196d7f0fff6444b90861fe8d609d",
                      "name": "testfile",
                      "content_type": content_type_1}

        obj_dict_2 = {"bytes": 24,
                      "last_modified": "2015-04-14T23:33:06.519020",
                      "hash": "ac0374ed4d43635f803c82469d0b5a10",
                      "name": "testfile2",
                      "content_type": content_type_2}

        listing = [obj_dict_1, obj_dict_2]
        fake_body = json.dumps(listing)

        resp = self._make_cont_get_req(fake_body, 'json', override=True)

        self.assertEqual('200 OK', resp.status)
        body = resp.body
        self.assertEqual(len(body), int(resp.headers['Content-Length']))
        body_json = json.loads(body)
        self.assertEqual(2, len(body_json))
        obj_dict_1['content_type'] = content_type_1
        self.assertDictEqual(obj_dict_1, body_json[0])
        obj_dict_2['content_type'] = content_type_2
        self.assertDictEqual(obj_dict_2, body_json[1])

    def test_cont_get_json_req_with_cipher_mismatch(self):
        content_type = 'image/jpeg'
        bad_crypto_meta = get_crypto_meta()
        bad_crypto_meta['cipher'] = 'unknown_cipher'

        obj_dict_1 = {"bytes": 16,
                      "last_modified": "2015-04-14T23:33:06.439040",
                      "hash": "c6e8196d7f0fff6444b90861fe8d609d",
                      "name": "testfile",
                      "content_type":
                          encrypt_and_append_meta(content_type,
                                                  crypto_meta=bad_crypto_meta)}

        listing = [obj_dict_1]
        fake_body = json.dumps(listing)

        resp = self._make_cont_get_req(fake_body, 'json')

        self.assertEqual('500 Internal Error', resp.status)
        # TODO: this error message is not appropriate, change
        self.assertEqual('Error decrypting header value', resp.body)

    def _assert_element_contains_dict(self, expected, element):
        for k, v in expected.items():
            entry = element.getElementsByTagName(k)
            self.assertIsNotNone(entry, 'Key %s not found' % k)
            actual = entry[0].childNodes[0].nodeValue
            self.assertEqual(v, actual,
                             "Expected %s but got %s for key %s"
                             % (v, actual, k))

    def test_cont_get_xml_req(self):
        content_type_1 = u'\uF10F\uD20D\uB30B\u9409'
        content_type_2 = 'text/plain; param=foo'

        fake_body = '''<?xml version="1.0" encoding="UTF-8"?>
<container name="testc">\
<object><hash>c6e8196d7f0fff6444b90861fe8d609d</hash><content_type>\
''' + encrypt_and_append_meta(content_type_1.encode('utf8')) + '''\
</content_type><name>testfile</name><bytes>16</bytes>\
<last_modified>2015-04-19T02:37:39.601660</last_modified></object>\
<object><hash>ac0374ed4d43635f803c82469d0b5a10</hash><content_type>\
''' + encrypt_and_append_meta(content_type_2.encode('utf8')) + '''\
</content_type><name>testfile2</name><bytes>24</bytes>\
<last_modified>2015-04-19T02:37:39.684740</last_modified></object>\
</container>'''

        resp = self._make_cont_get_req(fake_body, 'xml')
        self.assertEqual('200 OK', resp.status)
        body = resp.body
        self.assertEqual(len(body), int(resp.headers['Content-Length']))

        tree = minidom.parseString(body)
        containers = tree.getElementsByTagName('container')
        self.assertEqual(1, len(containers))
        self.assertEqual('testc',
                         containers[0].attributes.getNamedItem("name").value)

        objs = tree.getElementsByTagName('object')
        self.assertEqual(2, len(objs))

        obj_dict_1 = {"bytes": "16",
                      "last_modified": "2015-04-19T02:37:39.601660",
                      "hash": "c6e8196d7f0fff6444b90861fe8d609d",
                      "name": "testfile",
                      "content_type": content_type_1}
        self._assert_element_contains_dict(obj_dict_1, objs[0])
        obj_dict_2 = {"bytes": "24",
                      "last_modified": "2015-04-19T02:37:39.684740",
                      "hash": "ac0374ed4d43635f803c82469d0b5a10",
                      "name": "testfile2",
                      "content_type": content_type_2}
        self._assert_element_contains_dict(obj_dict_2, objs[1])

    def test_cont_get_xml_req_with_crypto_override(self):
        content_type_1 = 'image/jpeg'
        content_type_2 = 'text/plain; param=foo'

        fake_body = '''<?xml version="1.0" encoding="UTF-8"?>
<container name="testc">\
<object><hash>c6e8196d7f0fff6444b90861fe8d609d</hash>\
<content_type>''' + content_type_1 + '''\
</content_type><name>testfile</name><bytes>16</bytes>\
<last_modified>2015-04-19T02:37:39.601660</last_modified></object>\
<object><hash>ac0374ed4d43635f803c82469d0b5a10</hash>\
<content_type>''' + content_type_2 + '''\
</content_type><name>testfile2</name><bytes>24</bytes>\
<last_modified>2015-04-19T02:37:39.684740</last_modified></object>\
</container>'''

        resp = self._make_cont_get_req(fake_body, 'xml', override=True)

        self.assertEqual('200 OK', resp.status)
        body = resp.body
        self.assertEqual(len(body), int(resp.headers['Content-Length']))

        tree = minidom.parseString(body)
        containers = tree.getElementsByTagName('container')
        self.assertEqual(1, len(containers))
        self.assertEqual('testc',
                         containers[0].attributes.getNamedItem("name").value)

        objs = tree.getElementsByTagName('object')
        self.assertEqual(2, len(objs))

        obj_dict_1 = {"bytes": "16",
                      "last_modified": "2015-04-19T02:37:39.601660",
                      "hash": "c6e8196d7f0fff6444b90861fe8d609d",
                      "name": "testfile",
                      "content_type": content_type_1}
        self._assert_element_contains_dict(obj_dict_1, objs[0])
        obj_dict_2 = {"bytes": "24",
                      "last_modified": "2015-04-19T02:37:39.684740",
                      "hash": "ac0374ed4d43635f803c82469d0b5a10",
                      "name": "testfile2",
                      "content_type": content_type_2}
        self._assert_element_contains_dict(obj_dict_2, objs[1])

    def test_cont_get_xml_req_with_cipher_mismatch(self):
        content_type = 'image/jpeg'
        bad_crypto_meta = get_crypto_meta()
        bad_crypto_meta['cipher'] = 'unknown_cipher'

        fake_body = '''<?xml version="1.0" encoding="UTF-8"?>
<container name="testc">\
<object><hash>c6e8196d7f0fff6444b90861fe8d609d</hash><content_type>\
''' + encrypt_and_append_meta(content_type, crypto_meta=bad_crypto_meta) + '''\
</content_type><name>testfile</name><bytes>16</bytes>\
<last_modified>2015-04-19T02:37:39.601660</last_modified></object>\
</container>'''

        resp = self._make_cont_get_req(fake_body, 'xml')

        self.assertEqual('500 Internal Error', resp.status)
        self.assertEqual('Error decrypting header value', resp.body)


class TestModuleMethods(unittest.TestCase):
    def test_filter_factory(self):
        factory = decrypter.filter_factory({})
        self.assertTrue(callable(factory))
        self.assertIsInstance(factory(None), decrypter.Decrypter)


class TestDecrypter(unittest.TestCase):
    def test_app_exception(self):
        app = decrypter.Decrypter(
            FakeAppThatExcepts(), {})
        req = Request.blank('/', environ={'REQUEST_METHOD': 'GET'})
        with self.assertRaises(HTTPException) as catcher:
            req.get_response(app)
        self.assertEqual(catcher.exception.body,
                         FakeAppThatExcepts.get_error_msg())


if __name__ == '__main__':
    unittest.main()
