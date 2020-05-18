#! /usr/bin/python3.6
from pyverilog.vparser.parser import parse as rtl_parse
import pyverilog.vparser.ast as ast

import re
import json
import math
from collections import defaultdict
import subprocess
import os
import autopilot_parser
from assign_slr import assign_slr
import my_generator
from formator import *

class Edge:
  def __init__(self, name:str):
    self.src : Vertex = None
    self.dst : Vertex = None
    self.src_sub : Vertex = None
    self.dst_sub : Vertex = None
    self.width = -1
    self.depth = -1
    self.addr_width = -1
    self.name = name
    self.mark = False
    self.latency = 1 # default latency of 1

class Vertex:
  def __init__(self, type:str, name : str):
    self.in_edges = [] # stores Edge objects
    self.out_edges = []
    self.type = type
    self.name = name
    self.upstream = [] # marked edges in the upstream
    self.downstream = []
    self.area = autopilot_parser.Area(-1, -1, -1, -1)
    self.slr_loc = -1
    self.slr_sub_loc = -1
    self.sub_vertices = {} # pp id -> sub vertex
    self.actual_to_sub = {} # map actual edge name -> sub vertex
    print(f'*** init vertex {self.name} of type {self.type}')

  def add_in(self, edge : Edge):
    self.out_edges.append(edge)

  def add_out(self, edge : Edge):
    self.in_edges.append(edge)

class Graph:

  def __init__(self, formator):
    self.flag = 0
    self.formator = formator

    # must use list to wrap up the addr
    top_mod_ast, directives = rtl_parse([self.formator.top_hdl_path]) 

    self.vertices = {} # name -> Vertex
    self.edges = {} # name -> Edge
    self.edge_to_vertex = {} # raw edge name (xxx_din & xxx_dout) -> Vertex

    self.dfs(top_mod_ast, set(), self.initVertices)

    for e, v in self.edge_to_vertex.items():
      print(f'{e} -> {v.type} : {v.name}') if 'm_axi' in v.name else 0

    self.dfs(top_mod_ast, set(), self.initEdges)

    # for e in self.edges.values():
    #   print(f'{e.name}', end='')
    #   print(f'\t\t{e.src.name}\t->\t{e.dst.name}')
    # run ILP to solve the SLR assignment problem
    assign_slr(self.vertices.values(), self.edges.values(), self.formator)

    my_generator.generateConstraint_2D(self.formator, self.vertices.values(), self.edges.values())
    my_generator.generateTopHdl(self.formator, top_mod_ast, self.edges)
    
    # if (self.formator.target_dir):
    #   verilog_dir = f'{self.formator.target_dir}/{self.formator.top_name}/solution/syn/verilog/'
    #   top_rtl_file = f'./{self.formator.top_name}_{self.formator.top_name}.v'
      
    #   if (not os.path.isfile(f'{verilog_dir}/{top_rtl_file}')):
    #     print('error locating HLS projects')
    #     print(verilog_dir)
    #     print(top_rtl_file)
    #     exit

    #   subprocess.run(['mv', f'./{top_rtl_file}', verilog_dir])
    #   subprocess.run(['mv', f'./pack_xo.tcl', self.formator.target_dir])
    #   subprocess.run(['mv', f'./constraint.tcl', self.formator.target_dir])

  def showVertices(self):
    for v in self.vertices.values():
      print(f'{v.name}: {v.area}')
      for e in v.in_edges:
        print(f'  <- {e.name}')
      for e in v.out_edges:
        print(f'  -> {e.name}')

  def showEdges(self):
    for e in self.edges.values():
      print(f'{e.name}: {e.src.name} -> {e.dst.name}')

  #
  # traverse the rtl and apply a function
  #
  def dfs(self, node, visited, func):
    if(node not in visited):
      visited.add(node)
    else:
      return
    
    func(node)

    for c in node.children():
      self.dfs(c, visited, func)

  #
  # for each instance create a Vertex
  #
  def initVertices(self, node):
    # for every non-fifo module instance  
    if (not self.formator.isValidInstance(node)):
      return 
    if (self.formator.isFIFO(node)):
      return 

    # create async_mmap as standalone nodes but no edges
    # if ('async_mmap' in node.module):
    #   return

    v = Vertex(node.module, node.name)
    
    actual_to_formal = {} # map actual FIFO name to formal FIFO name
    for portarg in node.portlist:
      # filter out constant ports
      if(not isinstance(portarg.argname, ast.Identifier)):
        continue

      formal_raw = portarg.portname
      actual_raw = portarg.argname.name

      # each fifo xxx -> xxx_din & xxx_dout, each maps to a vertex
      if (('_dout' in formal_raw and '_dout' in actual_raw) or \
          ('_din' in formal_raw and '_din' in actual_raw) ):
        
        # map raw edge name to vertices
        self.edge_to_vertex[actual_raw] = v

        # map formal to actual
        formal_strip = self.formator.extractFIFOFromRaw(formal_raw)
        actual_strip = self.formator.extractFIFOFromRaw(actual_raw)

        actual_to_formal[actual_strip] = formal_strip

    # get area
    rpt_name = self.formator.getRptFile(v)
    v.area = autopilot_parser.getAreaFromReport(rpt_name)

    # split into pseudo vertex at loop level
    sche_file = self.formator.getScheFile(v.type)
    formal_to_pp, pp_to_formal = autopilot_parser.getGrouping(sche_file) # map pp id -> fifos used in this pp
    for i, pp in pp_to_formal.items():
      v.sub_vertices[i] = (Vertex(node.module, f'{node.name}_sub_{i}'))
    
    # map edge to pseudo vertices
    # we have actual_to_formal and formal_to_pp, need to bridge them
    for actual_strip, formal_strip in actual_to_formal.items():
      # print(actual_strip, formal_strip)
      # print(json.dumps(formal_to_pp, indent=2, sort_keys=True))

      # some FIFOs are not accessed in pp loops
      if (formal_strip in formal_to_pp.keys()):
        v.actual_to_sub[actual_strip] = v.sub_vertices[formal_to_pp[formal_strip] ]

    self.vertices[node.name] = v

  #
  # FIXME: for async_mmap
  #
  def initAsyncMmap(self, node):
    # for every non-fifo module instance  
    if (not self.formator.isValidInstance(node)):
      return 
    if ('async_mmap' not in node.module):
      return

    v = Vertex(node.module, node.name)
    
    actual_to_formal = {} # map actual FIFO name to formal FIFO name
    for portarg in node.portlist:
      # filter out constant ports
      if(not isinstance(portarg.argname, ast.Identifier)):
        continue

      formal_raw = portarg.portname
      actual_raw = portarg.argname.name

      # each fifo xxx -> xxx_din & xxx_dout, each maps to a vertex
      if (('_dout' in formal_raw and '_dout' in actual_raw) or \
          ('_din' in formal_raw and '_din' in actual_raw) ):
        
        # map raw edge name to vertices
        self.edge_to_vertex[actual_raw] = v

        # map formal to actual
        formal_strip = self.formator.extractFIFOFromRaw(formal_raw)
        actual_strip = self.formator.extractFIFOFromRaw(actual_raw)

        actual_to_formal[actual_strip] = formal_strip

    # get area
    v.area = autopilot_parser.Area(0, 0, 200, 200)

    self.vertices[node.name] = v

  #
  # for each FIFO or relay station create an Edge
  #
  def initEdges(self, node):
    # only considers fifo/rs instances
    # for TLP we need to consider async_mmap, which contains an implicity FIFO
    if (not self.formator.isValidInstance(node)):
      return 
    if (not self.formator.isFIFO(node)):
      #if (not self.formator.isAsyncMmap(node)):
        return 

    e = Edge(node.name)

    # extract width
    e.width = self.formator.extractFIFOWidth(node)
    e.depth = self.formator.extractFIFODepth(node)
    e.addr_width = int(math.log2(e.depth)+1)

    # extract wire name
    # augment vertices with edge info
    for portarg in node.portlist:
      # filter constant ports
      if(not isinstance(portarg.argname, ast.Identifier)):
        continue

      formal_raw = portarg.portname
      actual_raw = portarg.argname.name

      # set up edge
      if ('_dout' in actual_raw and '_dout' in formal_raw):
        e.dst = self.edge_to_vertex[actual_raw]
      elif ('_din' in actual_raw and '_din' in formal_raw):
        e.src = self.edge_to_vertex[actual_raw]

      # setup vertices
      if ('_dout' in actual_raw and '_dout' in formal_raw):
        e.dst.add_in(e)
      elif ('_din' in actual_raw and '_din' in formal_raw):
        e.src.add_out(e)   

      # setup sub vertices
      if ('_dout' in actual_raw and '_dout' in formal_raw):
        try:
          e.dst.actual_to_sub[e.name].add_in(e)
        except:
          print(f'non-pp edge: {e.name}')
      elif ('_din' in actual_raw and '_din' in formal_raw):
        try:
          e.src.actual_to_sub[e.name].add_out(e)         
        except:
          print(f'non-pp edge: {e.name}')

    self.edges[node.name] = e

        


  