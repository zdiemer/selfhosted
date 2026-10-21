// Watches hw7_Diemer.v's test_bench; never drives it.
//
// The 2014 testbench has no $display and no $finish: it was written to be read
// in ModelSim's waveform window, so on its own a command-line simulator runs
// its free-running clock forever and prints nothing. This is a SECOND root
// module, compiled alongside with `iverilog -s test_bench -s harness`, that
// reads the testbench's nets by hierarchical reference, prints them, and stops.
// The submitted file is compiled byte-for-byte.
//
// The `check` column is the harness's claim, not the student's: HW7P1 is a
// sum of products that, worked through, is exactly A < B for two 2-bit
// numbers, and the counter in the testbench walks all sixteen (A, B) pairs.
module harness;
  wire [1:0] a = {test_bench.HW7P1A1, test_bench.HW7P1A0};
  wire [1:0] b = {test_bench.HW7P1B1, test_bench.HW7P1B0};

  initial $display(" time  counter  A  B   O   A<B  check");

  // #1 after each count, so the combinational output has settled before it is
  // read. Without it the harness can sample O in the same timestep the counter
  // moved and report the previous row's answer.
  always @(test_bench.counterO) #1
    $display("%5t     %2d    %0d  %0d   %b    %b   %s", $time, test_bench.counterO,
             a, b, test_bench.HW7P1O, a < b,
             (a < b) == test_bench.HW7P1O ? "ok" : "MISMATCH");

  // Sixteen counts at 20 time units each, plus the wrap back to zero.
  initial #330 $finish;
endmodule
