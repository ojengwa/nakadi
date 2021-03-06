[![Build Status](https://travis-ci.org/zalando/nakadi.svg)](https://travis-ci.org/zalando/nakadi) [![ReviewNinja](https://app.review.ninja/44234368/badge)](https://app.review.ninja/zalando/nakadi)

[![Swagger API](http://online.swagger.io/validator?url=https://raw.githubusercontent.com/zalando/nakadi/master/nakadi/swagger.yaml)](http://online.swagger.io/validator/debug?url=https://raw.githubusercontent.com/zalando/nakadi/master/nakadi/swagger.yaml)

Nakadi Event Bus
=====================

> This is a prototype and a proof of concept project for now implemented in python.

The goal of the `nakadi` project (ნაკადი means `stream` in Georgian language) is to build an event bus infrastructure to:

*  enable convenient development of event-driven applications
*  securely and efficiently publish and consume events as easy as possible
*  abstract event exchange by a stanartized RESTful [API](/nakadi/swagger.yaml)

Some additional technical requirements that we wanted to cover by this architecture:

* event ordering guarantees
* fast (near real-time) event processing
* scalable and highly available architecture
* [STUPS](https://stups.io/) compatible

Additional topics, that we plan to cover in the near future are: 

* discoverability of the resource structures flowing into the event bus
* centralized discovery service, that will use these capabilities to collect resource schema information for easy lookup by developers

> NOTE: it is not really clear if the resource schema discoverability service should be part of `nakadi` event bus

What does the prototype already have?
=====================================

* [x] REST abstraction over Kafka-like queues
* [ ] support of event filtering per Subscription
* streaming/batching of events to/from the clients
  * [ ] creation of topics
  * [x] low-level interface
    * manual client side partition management is needed
    * no support of commits
  * [ ] high-level interface
    * automatic redistribution of partitions between consuming clients
    * commits should be issued to move server-side cursors

Running it locally
==================

To run the project locally

Install python requirements

    pip3 install -r requirements.txt

Run ZooKeeper and Kafka docker images

    make --directory local-test run
    
Run the local service

    python3 run_local.py
