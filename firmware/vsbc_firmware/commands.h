/*
  Serial command parser. Reads lines from Serial, splits them into a command
  and arguments, and runs the matching handler. docs/protocol.md is the spec.
*/
#pragma once

namespace commands {

void poll();   // call every loop()

}  // namespace commands
