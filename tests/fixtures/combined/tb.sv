`timescale 1ns/1ps

module tb;
  logic clk = 0;
  logic rst_n = 0;
  logic enable = 0;
  logic din = 0;
  logic dout;

  TOP dut(.*);

  always #5 clk = ~clk;

  initial begin
    $fsdbDumpfile("combined.fsdb");
    $fsdbDumpvars(0, tb);
    #12 rst_n = 1;
    #6 enable = 1;
    #4 din = 1;
    #20 din = 0;
    #20 $finish;
  end
endmodule
