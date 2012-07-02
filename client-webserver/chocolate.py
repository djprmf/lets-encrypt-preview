#!/usr/bin/env python

import web, redis, time
import CSR
from Crypto.Hash import SHA256, HMAC
from Crypto import Random 
from chocolate_protocol_pb2 import chocolatemessage
from google.protobuf.message import DecodeError

MaximumSessionAge = 100   # seconds, to demonstrate session timeout
MaximumChallengeAge = 600 # to demonstrate challenge timeout

urls = (
     '.*', 'session'
)

def sha256(m):
    return SHA256.new(m).hexdigest()

def hmac(k, m):
    return HMAC.new(k, m, SHA256).hexdigest()

def random():
    """Return 64 hex digits representing a new 32-byte random number."""
    return sha256(Random.get_random_bytes(32))

def safe(what, s):
    """Is string s within the allowed-character policy for this field?"""
    if not isinstance(s, basestring):
        return False
    if len(s) == 0:
        # No validated string should be empty.
        return False
    base64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    csr_ok = base64 + " =-"
#    if what == "nonce":
#        return s.isalnum()
    if what == "recipient" or what == "hostname":
        return all(c.isalnum() or c in "-." for c in s)
    elif what == "csr":
       return all(all(c in csr_ok for c in line) for line in s.split("\n"))
       # Note that this implies CSRs must have LF for end-of-line, not CRLF
    elif what == "session":
       return len(s) == 64 and all(c in "0123456789abcdef" for c in s)
    else:
       return False

sessions = redis.Redis()

class session(object):
    def __init__(self):
        self.id = None

    def exists(self):
        return self.id in sessions

    def live(self):
        return self.id in sessions and sessions.hget(self.id, "live") == "True"

    def state(self):
        # Should be:
        # * None for a session where the signing request has not
        #   yet been received;
        # * "makechallenge" where the CA is still coming up with challenges,
        # * "testchallenge" where the challenges have been issued,
        # * "issue" where the CA is in the process of issuing the cert,
        # * "done" where the cert has been issued.
        #
        # Note that this is independent of "live", which specifies whether
        # further actions involving this session are permitted.  When
        # sessions die, they currently keep their last state, but the
        # client can't cause their state to advance further.  For example,
        # if a session times out while waiting for the client to complete
        # a challenge, we have state="testchallenge", but live="False".
        return sessions.hget(self.id, "state")

    def create(self, timestamp=int(time.time())):
        if not self.exists():
            sessions.hset(self.id, "created", timestamp)
            sessions.hset(self.id, "live", True)
            sessions.lpush("active-requests", self.id)
        else:
            raise KeyError

    def kill(self):
        # It is now possible to get here via die() even if there is no session
        # ID, because we can die() on the initial request before a session ID
        # has been allocated!
        if self.id:
            sessions.hset(self.id, "live", False)
            sessions.lrem("active-requests", self.id)

    def destroy(self):
        sessions.lrem("active-requests", self.id)
        sessions.delete(self.id)

    def age(self):
        return int(time.time()) - int(sessions.hget(self.id, "created"))

    def request_made(self):
        """Has there already been a signing request made in this session?"""
        return sessions.hget(self.id, "state") is not None

    def add_request(self, csr, names):
        sessions.hset(self.id, "csr", csr)
        for name in names: sessions.lpush(self.id + ":names", name)
        sessions.hset(self.id, "state", "makechallenge")
        sessions.lpush("pending-makechallenge", self.id)
        return True

    def challenges(self):
        n = int(sessions.hget(self.id, "challenges"))
        for i in xrange(n):
            yield r.hgetall("session:%d" % i)

    def make_challenge(self):
        challid = random()
        value = random()
        sessions.hset(self.id + ":req", "id", challid)
        sessions.hset(self.id + ":req", "challtime", int(time.time()))
        sessions.hset(self.id + ":req", "challenge", value)
        return (challid, value)

    def handlesession(self, m, r):
        if r.failure.IsInitialized(): return
        if m.session == "":
            # New session
            r.session = random()
            self.id = r.session
            if not self.exists():
                self.create()
                self.handlenewsession(m, r)
            else:
                raise ValueError, "new random session already existed!"
        elif m.session and not r.failure.IsInitialized():
            if not safe("session", m.session):
                # Note that self.id is still uninitialized here.
                self.die(r, r.BadRequest, uri="https://ca.example.com/failures/illegalsession")
                return
            self.id = m.session
            r.session = m.session
            if not (self.exists() and self.live()):
                # Don't need to, or can't, kill nonexistent/already dead session
                r.failure.cause = r.StaleRequest
            elif self.age() > MaximumSessionAge:
                self.die(r, r.StaleRequest)
            else:
                self.handleexistingsession(m, r)

    def handlenewsession(self, m, r):
        if r.failure.IsInitialized(): return
        if not m.request.IsInitialized():
            # It is mandatory to make a signing request at the outset of a session.
            self.die(r, r.BadRequest, uri="https://ca.example.com/failures/missingrequest")
            return
        if self.request_made():
            # Can't make new signing requests if there have already been requests in
            # this session.  (All signing requests should occur together at the
            # beginning.)
            self.die(r, r.BadRequest, uri="https://ca.example.com/failures/priorrequest")
            return
        # Process the request.
        # TODO: check client puzzle before processing request
        timestamp = m.request.timestamp
        recipient = m.request.recipient
        csr = m.request.csr
        sig = m.request.sig
        if not all([safe("recipient", recipient), safe("csr", csr)]):
            self.die(r, r.BadRequest, uri="https://ca.example.com/failures/illegalcharacter")
            return
        if timestamp > time.time() or time.time() - timestamp > 100:
            self.die(r, r.BadRequest, uri="https://ca.example.com/failures/time")
            return
        if recipient != "ca.example.com":
            self.die(r, r.BadRequest, uri="https://ca.example.com/failures/recipient")
            return
        if not CSR.parse(csr):
            self.die(r, r.BadCSR)
            return
        if CSR.verify(CSR.pubkey(csr), sig) != sha256("(%d) (%s) (%s)" % (timestamp, recipient, csr)):
            self.die(r, r.BadSignature)
            return
        if not CSR.csr_goodkey(csr):
            self.die(r, r.UnsafeKey)
            return
        names = CSR.subject_names(csr)
        for san in names:  # includes CN as well as SANs
            if not safe("hostname", san) or not CSR.can_sign(san):
                # TODO: Is there a problem including client-supplied data in the URL?
                self.die(r, r.CannotIssueThatName, uri="https://ca.example.com/failures/name?%s" % san)
                return
        # Phew!
        self.add_request(csr, names)
        # This version is relying on an external daemon process to create
        # the challenges.  If we want to create them ourselves, we have to
        # do what the daemon does, and then return the challenges instead
        # of returning proceed.
        r.proceed.timestamp = int(time.time())
        r.proceed.polldelay = 10

    def handleexistingsession(self, m, r):
        if m.request.IsInitialized():
            self.die(r, r.BadRequest, uri="https://ca.example.com/failures/requestinexistingsession")
            return
        # The caller has verified that this session exists and is live.
        # If we have no state, something is crazy (maybe a race from two
        # instances of the client?).
        state = self.state()
        if state is None:
            self.die(r, r.BadRequest, uri="https://ca.example.com/failures/uninitializedsession")
            return
        # If we're in makechallenge or issue, tell the client to come back later.
        if state == "makechallenge" or state == "issue":
            r.proceed.timestamp = int(time.time())
            r.proceed.polldelay = 10
        return
        # If we're in testchallenge, tell the client about the challenges and their
        # current status.
        if state == "testchallenge":
            self.send_challenges(m, r)
            return
        # If we're in done, tell the client to come back later.
        pass
        # Unknown session status.
        self.die(r, r.BadRequest, uri="https://ca.example.com/failures/internalerror")
        return
        # TODO: Process challenge-related messages from the client.

    def die(self, r, reason, uri=None):
        self.kill()
        r.failure.cause = reason
        if uri: r.failure.URI = uri

    def handleclientfailure(self, m, r):
        if r.failure.IsInitialized(): return
        if m.failure.IsInitialized():
            # Received failure message from client!
            self.die(r, r.AbandonedRequest)

    def send_challenges(self, m, r):
        if r.failure.IsInitialized(): return
        # TODO: This needs a more sophisticated notion of success/failure,
        # and also of the possibility of multiple data strings.
        for c in self.challenges():
            chall = r.challenges.add()
            chall.type = int(c["type"])
            chall.name = c["name"]
            chall.satisfied = c["satisfied"]
            chall.succeeded = c["succeeded"]
            chall.data.append(c["data"])
      
    def POST(self):
        web.header("Content-type", "application/x-protobuf+chocolate")
#        web.setcookie("chocolate", hmac("foo", "bar"),
#                       secure=True) # , httponly=True)
        m = chocolatemessage()
        r = chocolatemessage()
        r.chocolateversion = 1
        try:
            m.ParseFromString(web.data())
        except DecodeError:
            r.failure.cause = r.BadRequest
        else:
            if m.chocolateversion != 1:
                r.failure.cause = r.UnsupportedVersion

        self.handleclientfailure(m, r)

        self.handlesession(m, r)

        # Send reply
        if m.debug:
            web.header("Content-type", "text/plain")
            return "SAW MESSAGE: %s\nRESPONSE: %s\n" % (str(m), str(r))
        else:
            return r.SerializeToString()

    def GET(self):
        web.header("Content-type", "text/html")
        return "Hello, world!  This server only accepts POST requests."

if __name__ == "__main__":
    app = web.application(urls, globals())
    app.run()
