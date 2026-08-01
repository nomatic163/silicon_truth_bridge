`include "stb_defs.svh"

module TOP(
  input  logic clk,
  input  logic din,
  output logic dout
);
  `STB_ASSIGN(dout, din);
endmodule
