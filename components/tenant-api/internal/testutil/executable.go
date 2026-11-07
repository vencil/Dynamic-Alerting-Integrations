package testutil

import (
	"os"
	"syscall"
)

// WriteExecutable is os.WriteFile for a file a test is about to run (a stub
// git, a hook, a driver program). Use it whenever perm has an execute bit;
// TestExecutableWritesGoThroughWriteExecutable keeps os.WriteFile and
// os.OpenFile from being used for that in this module's tests.
//
// os.WriteFile races with any parallel test that starts a process
// (golang/go#22315): a fork landing between the file's open and close copies
// the write descriptor into the child, which holds it until it execs (the
// descriptor is close-on-exec, so it is closed only then). Running the file
// in that window fails with ETXTBSY, "text file busy". os/exec forks with
// syscall.ForkLock held for writing; holding it for reading from open to
// close keeps every fork out of that window. A child forked before the open
// never had the descriptor and one forked after the close never sees it, so
// this holds on every Unix, however long the child takes to exec.
func WriteExecutable(name string, data []byte, perm os.FileMode) error {
	syscall.ForkLock.RLock()
	defer syscall.ForkLock.RUnlock()
	f, err := os.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, perm)
	if err != nil {
		return err
	}
	_, err = f.Write(data)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	return err
}
