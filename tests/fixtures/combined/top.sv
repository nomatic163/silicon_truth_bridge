`timescale 1ns/1ps

module TOP(
  input  logic clk,
  input  logic rst_n,
  input  logic enable,
  input  logic din,
  output logic dout
);
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)
      dout <= 1'b0;
    else if (enable)
      dout <= din;
  end
endmodule
