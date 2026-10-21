// Watches hw8_Diemer.v's test_bench; never drives it. See harness_hw7.v for
// why this exists as a second root module rather than an edit to the testbench.
//
// The last column exists because of the 2014 screenshot. ModelSim displayed the
// 3-bit bus with a signed radix, so the Gray sequence 0 1 3 2 6 7 5 4 appears in
// hw8_Diemer.PNG as 0 1 3 2 -2 -1 -3 -4. Printing both lets the two be compared
// digit for digit.
module harness;
  task row;
    $display("%5t   %b  %b  %b      %0d          %0d", $time,
             test_bench.HW8out[2], test_bench.HW8out[1], test_bench.HW8out[0],
             test_bench.HW8out, $signed(test_bench.HW8out));
  endtask

  initial $display(" time  S2 S1 S0  as 3 bits  as ModelSim showed it");
  initial #1 row;
  always @(posedge test_bench.clk) #1 row;

  // Two full trips round the eight states.
  initial #340 $finish;
endmodule
