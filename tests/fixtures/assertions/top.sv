module top (
  input  logic clk,
  input  logic rst_n,
  input  logic req,
  input  logic ack,
  input  logic ready
);
  property p_fixed_delay;
    @(posedge clk) disable iff (!rst_n)
      req |-> ##2 ack;
  endproperty

  a_fixed_delay: assert property (p_fixed_delay);

  a_range_delay: assert property (
    @(posedge clk) disable iff (!rst_n)
      $rose(req) |=> ##[1:3] (ack && ready)
  );
endmodule
