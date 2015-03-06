#
# Copyright CEA/DAM/DIF (2009-2015)
#  Contributor: Stephane THIELL <stephane.thiell@cea.fr>
#
# This file is part of the ClusterShell library.
#
# This software is governed by the CeCILL-C license under French law and
# abiding by the rules of distribution of free software.  You can  use,
# modify and/ or redistribute the software under the terms of the CeCILL-C
# license as circulated by CEA, CNRS and INRIA at the following URL
# "http://www.cecill.info".
#
# As a counterpart to the access to the source code and  rights to copy,
# modify and redistribute granted by the license, users are provided only
# with a limited warranty  and the software's author,  the holder of the
# economic rights,  and the successive licensors  have only  limited
# liability.
#
# In this respect, the user's attention is drawn to the risks associated
# with loading,  using,  modifying and/or developing or reproducing the
# software by the user in light of its specific status of free software,
# that may mean  that it is complicated to manipulate,  and  that  also
# therefore means  that it is reserved for developers  and  experienced
# professionals having in-depth computer knowledge. Users are therefore
# encouraged to load and test the software's suitability as regards their
# requirements in conditions enabling the security of their systems and/or
# data to be ensured and,  more generally, to use and operate it in the
# same conditions as regards security.
#
# The fact that you are presently reading this means that you have had
# knowledge of the CeCILL-C license and that you accept its terms.

"""
A ClusterShell Engine using epoll, an I/O event notification facility.

The epoll event distribution interface is available on Linux 2.6, and
has been included in Python 2.6.
"""

import errno
import select
import time

from ClusterShell.Engine.Engine import Engine, E_READ, E_WRITE
from ClusterShell.Engine.Engine import EngineNotSupportedError
from ClusterShell.Engine.Engine import EngineTimeoutException
from ClusterShell.Worker.EngineClient import EngineClientEOF


class EngineEPoll(Engine):
    """
    EPoll Engine

    ClusterShell Engine class using the select.epoll mechanism.
    """

    identifier = "epoll"

    def __init__(self, info):
        """
        Initialize Engine.
        """
        Engine.__init__(self, info)
        try:
            # get an epoll object
            self.epolling = select.epoll()
        except AttributeError:
            raise EngineNotSupportedError(EngineEPoll.identifier)

    def release(self):
        """Release engine-specific resources."""
        self.epolling.close()

    def _register_specific(self, fd, event):
        """Engine-specific fd registering. Called by Engine register."""
        if event & E_READ:
            eventmask = select.EPOLLIN
        else:
            assert event & E_WRITE
            eventmask = select.EPOLLOUT

        self.epolling.register(fd, eventmask)

    def _unregister_specific(self, fd, ev_is_set):
        """
        Engine-specific fd unregistering. Called by Engine unregister.
        """
        self._debug("UNREGSPEC fd=%d ev_is_set=%x"% (fd, ev_is_set))
        if ev_is_set:
            self.epolling.unregister(fd)

    def _modify_specific(self, fd, event, setvalue):
        """
        Engine-specific modifications after a interesting event change for
        a file descriptor. Called automatically by Engine set_events().
        For the epoll engine, it modifies the event mask associated to a file
        descriptor.
        """
        self._debug("MODSPEC fd=%d event=%x setvalue=%d" % (fd, event,
                                                            setvalue))
        if setvalue:
            self._register_specific(fd, event)
        else:
            self.epolling.unregister(fd)

    def runloop(self, timeout):
        """
        Run epoll main loop.
        """
        if not timeout:
            timeout = -1

        start_time = time.time()

        # run main event loop...
        while self.evlooprefcnt > 0:
            self._debug("LOOP evlooprefcnt=%d (reg_clifds=%s) (timers=%d)" % \
                    (self.evlooprefcnt, self.reg_clifds.keys(),
                     len(self.timerq)))
            try:
                timeo = self.timerq.nextfire_delay()
                if timeout > 0 and timeo >= timeout:
                    # task timeout may invalidate clients timeout
                    self.timerq.clear()
                    timeo = timeout
                elif timeo == -1:
                    timeo = timeout

                self._current_loopcnt += 1
                evlist = self.epolling.poll(timeo + 0.001)

            except IOError, ex:
                # might get interrupted by a signal
                if ex.errno == errno.EINTR:
                    continue

            for fd, event in evlist:

                # get client instance
                client, stream = self._fd2client(fd)
                if client is None:
                    continue

                fdev = stream.evmask
                sname = stream.name

                # set as current processed client
                self._current_client = client

                # check for poll error condition of some sort
                if event & select.EPOLLERR:
                    self._debug("EPOLLERR fd=%d sname=%s fdev=0x%x (%s)" % \
                                (fd, sname, fdev, client))
                    assert fdev & E_WRITE
                    self.remove_stream(client, stream)
                    self._current_client = None
                    continue

                # check for data to read
                if event & select.EPOLLIN:
                    assert fdev & E_READ
                    assert stream.events & fdev, (stream.events, fdev)
                    self.modify(client, sname, 0, fdev)
                    try:
                        client._handle_read(sname)
                    except EngineClientEOF:
                        self._debug("EngineClientEOF %s %s" % (client, sname))
                        self.remove_stream(client, stream)
                        self._current_client = None
                        continue

                # or check for end of stream (do not handle both at the same
                # time because handle_read() may perform a partial read)
                elif event & select.EPOLLHUP:
                    assert fdev & E_READ, "fdev 0x%x & E_READ" % fdev
                    self._debug("EPOLLHUP fd=%d sname=%s %s (%s)" % \
                                (fd, sname, client, client.streams))
                    self.remove_stream(client, stream)
                    self._current_client = None
                    continue

                # check for writing
                if event & select.EPOLLOUT:
                    self._debug("EPOLLOUT fd=%d sname=%s %s (%s)" % \
                                (fd, sname, client, client.streams))
                    assert fdev & E_WRITE
                    assert stream.events & fdev, (stream.events, fdev)
                    self.modify(client, sname, 0, fdev)
                    client._handle_write(sname)

                self._current_client = None

                # apply any changes occured during processing
                if client.registered:
                    self.set_events(client, stream)

            # check for task runloop timeout
            if timeout > 0 and time.time() >= start_time + timeout:
                raise EngineTimeoutException()

            # process clients timeout
            self.fire_timers()

        self._debug("LOOP EXIT evlooprefcnt=%d (reg_clifds=%s) (timers=%d)" % \
                (self.evlooprefcnt, self.reg_clifds, len(self.timerq)))

