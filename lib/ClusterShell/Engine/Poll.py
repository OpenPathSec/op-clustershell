# EnginePdsh.py -- ClusterShell pdsh engine with poll()
# Copyright (C) 2007, 2008 CEA
#
# This file is part of shine
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
#
# $Id: EnginePdsh.py 11 2008-01-11 15:19:44Z st-cea $

from Engine import Engine

import select
import os
import thread


class EnginePoll(Engine):
    """
    Poll Engine

    ClusterShell engine using the select.poll mechanism (Linux poll() syscall).
    """
    def __init__(self, info):
        """
        Initialize Engine.
        """
        Engine.__init__(self, info)
        try:
            # Get a polling object
            self.polling = select.poll()
        except:
            print "Fatal error: select.poll() not supported?"
            raise
        self.workers = {}
        self.dictout = {}
        self.running = False
        self.run_lock = thread.allocate_lock()
        self.start_lock = thread.allocate_lock()
        self.start_lock.acquire()

    """
    def add_msg(self, worker, nodename, msg):
        worker.add_node_msg(nodename, msg)
    """

    def set_rc(self, worker, nodename, retcode):
        worker.set_node_rc(nodename, retcode)
    
    def register(self, worker):
        """Register a worker for input I/O"""
        fd = worker.fileno()
        self.polling.register(fd, select.POLLIN)
        self.workers[fd] = worker

    def unregister(self, worker):
        """Unregister a worker"""
        fd = worker.fileno()
        self.polling.unregister(fd)
        del self.workers[fd]
        worker.close()

    def add(self, worker):
        Engine.add(self, worker)
        if self.running:
            self.register(worker.start())

    def run(self, timeout):
        """
        Pdsh engine run(): start workers and properly get replies
        """
        self.dictout = {}
        self.workers = {}

        # Start workers and register them in the poll()-based engine
        for worker in self.worker_list:
            self.register(worker.start())

        if timeout == 0:
            timeout = -1

        status = self.run_lock.acquire(0)
        assert status == True, "cannot acquire run lock"
        self.running = True

        self.start_lock.release()

        try:
            # Run main event loop
            while len(self.workers) > 0:

                # Wait for I/O
                evlist = self.polling.poll(timeout * 1000)

                # No event means timed out
                if len(evlist) == 0:
                    print "error timeout"
                    return

                for fd, event in evlist:

                    # Get worker object
                    worker = self.workers[fd]

                    # check for poll error
                    if event & select.POLLERR:
                        print "POLLERR"
                        self.unregister(worker)
                        continue

                    if event & select.POLLIN:
                        worker.handle_read()

                    # check for hung hup (EOF)
                    if event & select.POLLHUP:
                        #print "POLLHUP"
                        self.unregister(worker)
                        continue

                    assert event & select.POLLIN, "poll() returned without data to read"
        finally:
            self.running = False
            self.run_lock.release()

    def join(self):
        print "join"
        self.start_lock.acquire()
        self.start_lock.release()
        self.run_lock.acquire()
        self.run_lock.release()

    """
    def read(self, node):
        result = ""
        for worker in node.wl:
            if worker in self.worker_list:
                result += worker.read_node_buffer(str(node))
        return result

    def retcode(self, node):
        result = 0
        for worker in node.wl:
            if worker in self.worker_list:
                rc = worker.get_node_rc(str(node))
                if rc > result:
                    result = rc
        return result
    """

